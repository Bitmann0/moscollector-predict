"""Reproduce the independent D/A_link review without changing pilot artifacts.

Preflight works without customer data and audits the checked-in reports.
Historical mode requires prepared parquet inputs and uses weekly CPU refits,
point-in-time labels and persistent per-policy recommendation histories.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import time
from pathlib import Path

try:  # Unix only; on Windows peak RSS is reported as null
    import resource
except ImportError:
    resource = None

import duckdb
import polars as pl

from mkl import calibrate, cv, serve, train
from mkl import second_ml_audit as audit
from mkl.config import EQUIPMENT_STYPES, EXCLUDED_PERIODS

ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "f67f5d4c9d8c6c2472af104580644ca8f9d8425d"
INPUTS = ("interim/daily_channel.parquet", "interim/episodes.parquet",
          "interim/channels.parquet", "features/sensor.parquet")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inherited_evidence() -> dict:
    """Arithmetic checks of saved artifacts; these are NOT new ML results."""
    result = {"source": "checked_in_reports_not_rerun", "heads": {},
              "infeasible_baseline_emissions": [], "link_episode_overcount": []}
    for head, filename in [("D", "d_live_policy_temporal.json"),
                           ("A_link", "a_link_live_policy_temporal.json")]:
        path = ROOT / "reports" / filename
        report = json.loads(path.read_text(encoding="utf-8"))
        totals = {"alerts": 0, "hits": 0, "unknown_alerts": 0,
                  "candidates": 0, "unknown_candidates": 0}
        for fold in report["folds"]:
            model = (fold["model"] if head == "D" else
                     fold["policies"]["min_precision_0.50"]["model"])
            totals["alerts"] += model["alerts"]
            totals["hits"] += model.get("hits", model.get("true_alerts", 0))
            totals["unknown_alerts"] += model.get("unknown_alerts", 0)
            totals["candidates"] += fold["known_rows"] + fold["unknown_rows"]
            totals["unknown_candidates"] += fold["unknown_rows"]
            pairs = ([("min_precision_0.70", fold["baseline_threshold_selection"],
                       fold["baseline_thresholded"])] if head == "D" else
                     [(name, policy.get("baseline_selection"), policy["baseline"])
                      for name, policy in fold["policies"].items()])
            for name, pick, baseline in pairs:
                if pick and not pick["feasible"] and baseline["alerts"]:
                    result["infeasible_baseline_emissions"].append({
                        "head": head, "test_start": fold["test_start"],
                        "policy": name, "alerts": baseline["alerts"]})
            if head == "A_link" and model["episodes_caught"] > model["hits"]:
                result["link_episode_overcount"].append({
                    "test_start": fold["test_start"], "hits": model["hits"],
                    "episodes_caught": model["episodes_caught"]})
        totals["reported_precision"] = totals["hits"] / totals["alerts"]
        totals["source_sha256"] = sha256(path)
        result["heads"][head] = totals
    return result


def preflight(data_root: Path) -> dict:
    files = [{"path": name, "exists": (data_root/name).is_file()}
             for name in INPUTS]
    return {"schema_version": 1, "base_commit": BASE_COMMIT,
            "status": "ready" if all(f["exists"] for f in files) else "blocked_missing_data",
            "inputs": files, "historical_metrics": None,
            "inherited_evidence": inherited_evidence()}


def period(frame: pl.DataFrame, start: dt.date, end: dt.date) -> pl.DataFrame:
    return frame.filter(pl.col("day").is_between(start, end))


def masked_frame(features, outcomes, head, start, end, asof):
    return audit.candidates(period(features, start, end), outcomes, head, asof)


def score(frame, names, model, iso, baseline=None):
    if frame.is_empty():
        return frame.select("ch", "obj", "day", "y").with_columns(
            pl.lit(0.0, dtype=pl.Float64).alias("risk"))
    values = (frame[baseline].fill_null(0).to_numpy().astype(float) if baseline else
              calibrate.apply(iso, model.predict_proba(train._matrix(frame, names))[:, 1]))
    return frame.select("ch", "obj", "day", "y").with_columns(pl.Series("risk", values))


def evaluate_head(con, features, head, cfg, end, *, n_splits=5, test_days=90,
                  refresh_days=7, min_alerts=30, model_params=None,
                  training_start=dt.date(2023, 1, 1), reference_channels=None,
                  reference_ids=None):
    """Public test seam: identical runner is smoke-tested using generated data."""
    horizon = int(cfg["horizon_days"])
    if head not in {"D", "A_link"} or horizon != (7 if head == "D" else 1):
        raise ValueError("unsupported audit target/horizon")
    if head == "A_link" and cfg.get("variant") != "L9c":
        raise ValueError("independent link audit requires L9c")
    source_last = con.execute("SELECT max(day) FROM daily_channel").fetchone()[0]
    if end + dt.timedelta(days=horizon) > source_last:
        raise ValueError("test end exceeds mature source history")
    if refresh_days < 1 or refresh_days + horizon > int(cfg["max_model_lag_days"]):
        raise ValueError("refresh interval exceeds pilot model-lag contract")
    outcomes, events = audit.build_outcomes(con, head)
    events = events.filter(pl.col("available_on") <= source_last)
    baseline = "gap_vs_own_rhythm" if head == "A_link" else "n_bad_w7"
    if baseline not in features.columns:
        raise ValueError(f"missing baseline feature: {baseline}")
    drop = cfg.get("drop_feature_prefixes", [])
    features = features.select([c for c in features.columns
                                if not any(c.startswith(p) for p in drop)])
    if head == "D":
        features = features.filter(pl.col("stype").is_in(EQUIPMENT_STYPES))
    # Feature names are fixed before outcomes/availability metadata are joined.
    names = train.feature_columns(features)
    if not names:
        raise ValueError("no numeric features")
    first = end - dt.timedelta(days=n_splits * test_days - 1)
    if first - dt.timedelta(days=cfg["embargo_days"] + 61 + horizon) <= training_start:
        raise ValueError("insufficient training history before first calibration window")
    policies = {"model_50": (False, .5), "baseline_50": (True, .5),
                "model_70": (False, .7), "baseline_70": (True, .7),
                "model_topk": (False, None), "baseline_topk": (True, None)}
    histories = {key: [] for key in policies}
    selections, issued, scored_candidates = [], {key: [] for key in policies}, []
    cursor = first
    while cursor <= end:
        stop = min(end, cursor+dt.timedelta(days=refresh_days-1))
        asof = cursor-dt.timedelta(days=1)
        dates = cv.live_windows(cursor-dt.timedelta(days=horizon+1), cfg["embargo_days"])
        training = masked_frame(features, outcomes, head, training_start,
                                dates["training_end"], dates["calibration_start"]-dt.timedelta(days=1))
        for a, b in EXCLUDED_PERIODS:
            training = training.filter(~pl.col("day").is_between(a, b))
        training = training.filter(pl.col("y").is_not_null())
        calibration = masked_frame(features, outcomes, head, dates["calibration_start"],
                                   dates["calibration_end"], dates["threshold_start"]-dt.timedelta(days=1))
        calibration = calibration.filter(pl.col("y").is_not_null())
        threshold = masked_frame(features, outcomes, head, dates["threshold_start"],
                                 dates["threshold_end"], asof)
        test = masked_frame(features, outcomes, head, cursor, stop, source_last)
        scored_candidates.append(test.select("ch", "obj", "day", "y"))
        timing = {"fit_cpu_seconds": 0.0, "fit_wall_seconds": 0.0,
                  "score_cpu_seconds": 0.0, "score_wall_seconds": 0.0}
        model, iso = None, None
        started, cpu = time.perf_counter(), time.process_time()
        if training["y"].n_unique() == 2 and calibration["y"].n_unique() == 2:
            pos = training["y"].sum()
            params = {**(train.params_for(cfg, "lgbm") or {}), **(model_params or {})}
            model = train._build_model("lgbm", params, (training.height-pos)/pos)
            model.fit(train._matrix(training, names), training["y"].to_numpy())
            iso = calibrate.fit_isotonic(
                model.predict_proba(train._matrix(calibration, names))[:, 1],
                calibration["y"].to_numpy())
        timing.update(fit_cpu_seconds=time.process_time()-cpu,
                      fit_wall_seconds=time.perf_counter()-started)
        scored = {}
        started, cpu = time.perf_counter(), time.process_time()
        for is_baseline in (False, True):
            if not is_baseline and model is None:
                continue
            scored[is_baseline] = tuple(audit.daily_top(
                score(block, names, model, iso, baseline if is_baseline else None),
                cfg["budget_per_day"], bool(cfg.get("budget_per_object")))
                for block in (threshold, test))
        timing.update(score_cpu_seconds=time.process_time()-cpu,
                      score_wall_seconds=time.perf_counter()-started)
        row = {"start": str(cursor), "end": str(stop), "available_asof": str(asof),
               "windows": {k: str(v) for k, v in dates.items()},
               "training_rows": training.height, "calibration_rows": calibration.height,
               "threshold_candidates": threshold.height,
               "threshold_unknown": threshold["y"].null_count(), "policies": {}, **timing}
        for key, (is_baseline, minimum) in policies.items():
            started, cpu = time.perf_counter(), time.process_time()
            if is_baseline not in scored:
                pick = {"feasible": False, "threshold": None,
                        "reason": "insufficient known classes for fitting/calibration"}
                top = test.select("ch", "obj", "day", "y").head(0).with_columns(
                    pl.lit(0.0).alias("risk"))
            else:
                threshold_top, top = scored[is_baseline]
                pick = (audit.select_threshold(threshold_top, minimum, min_alerts)
                        if minimum is not None else {"feasible": True, "threshold": 0.0})
                # Both declared baselines are nonnegative; reject broken inputs.
                if is_baseline and any((block["risk"].min() or 0) < 0
                                       for block in (threshold_top, top)):
                    raise ValueError("baseline counts/ratios must be nonnegative")
            selection = audit.replay(top, pick["threshold"], history=histories[key])
            histories[key].extend(selection.filter(pl.col("alert")).select("ch", "day").iter_rows())
            histories[key] = [(ch, day) for ch, day in histories[key]
                              if (stop-day).days <= 7]
            issued[key].append(selection)
            row["policies"][key] = {**pick, "policy_cpu_seconds": time.process_time()-cpu,
                                   "policy_wall_seconds": time.perf_counter()-started}
        selections.append(row)
        print(json.dumps({"head": head, "refresh": str(cursor),
                          "candidates": test.height, "model_fitted": model is not None}), flush=True)
        cursor = stop + dt.timedelta(days=1)
    full = pl.concat(scored_candidates)
    totals = {key: pl.concat(parts) for key, parts in issued.items()}
    def summaries(start, stop):
        return {key: audit.summarize(full, selected, events, start, stop, horizon,
                                     reference_channels, reference_ids)
                for key, selected in totals.items()}
    return {"head": head, "label": "L9c_available_at_return" if head == "A_link" else "D_observed_v1",
            "target": ("Начало необычного пропуска телеметрии завтра" if head == "A_link" else
                       "Записанный сигнал тревоги/неисправности оборудования в следующие 7 суток"),
            "budget_per_day": cfg["budget_per_day"], "cooldown_days": 7,
            "refit_every_days": refresh_days, "no_backfill": True,
            "minimum_issued_alerts_for_threshold": min_alerts,
            "model_params": {**train.DEFAULT_PARAMS,
                             **(train.params_for(cfg, "lgbm") or {}), **(model_params or {})},
            "journal_initialization": "empty at first test day; carried across every refit and fold",
            "threshold_journal_initialization": "empty at start of each retrospective selection window",
            "features": names, "refreshes": selections,
            "total": summaries(first, end),
            "folds": [{"start": str(a), "end": str(b), "policies": summaries(a, b)}
                      for i in range(n_splits)
                      for a in [first+dt.timedelta(days=i*test_days)]
                      for b in [a+dt.timedelta(days=test_days-1)]],
            "caution": "Previously examined dates are not a blind test; prepared features and current "
                       "catalog are assumed as-of safe. Rebuild/verify from raw events before product approval."}


def historical(data_root, *, n_splits=5, test_days=90, threads=4, refresh_days=7,
               end_limit: dt.date | None = None):
    status = preflight(data_root)
    if status["status"] != "ready":
        return status
    started, cpu = time.perf_counter(), time.process_time()
    con = duckdb.connect(":memory:")
    con.execute(f"SET threads={int(threads)}")
    try:
        for name in ("daily_channel", "episodes"):
            con.read_parquet(str(data_root/"interim"/f"{name}.parquet")).create_view(name)
        source_last = con.execute("SELECT max(day) FROM daily_channel").fetchone()[0]
        features = (pl.scan_parquet(data_root/"features"/"sensor.parquet")
                    .filter(pl.col("day") >= dt.date(2023, 1, 1))
                    .with_columns(pl.col(pl.Float64).cast(pl.Float32))
                    .collect())
        channels = pl.read_parquet(data_root/"interim"/"channels.parquet")
        configs = serve.load_heads()
        results = []
        for head in ("D", "A_link"):
            cfg = configs[head]
            end = min(features["day"].max(), source_last-dt.timedelta(days=cfg["horizon_days"]))
            if end_limit is not None:  # валидация до отложенного периода
                end = min(end, end_limit)
            reference = channels.filter(pl.col("stype").is_in(EQUIPMENT_STYPES)) if head == "D" else channels
            results.append(evaluate_head(con, features, head, cfg, end,
                           n_splits=n_splits, test_days=test_days, refresh_days=refresh_days,
                           model_params={"n_jobs": threads}, reference_channels=reference["ch"].n_unique(),
                           reference_ids=set(reference["ch"].to_list())))
    finally:
        con.close()
    rss = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
           if resource is not None else None)
    return {**status, "status": "historical_run_complete", "historical_metrics": results,
            "data_sha256": {name: sha256(data_root/name) for name in INPUTS},
            "runtime": {"cpu_seconds": time.process_time()-cpu,
                        "wall_seconds": time.perf_counter()-started,
                        "peak_rss_bytes": (None if rss is None else
                                           rss if platform.system() == "Darwin" else rss*1024),
                        "platform": platform.platform(), "python": platform.python_version(),
                        "threads": threads,
                        "versions": {name: importlib.metadata.version(name) for name in
                                     ("duckdb", "polars", "numpy", "lightgbm", "scikit-learn")}}}


def markdown_report(result: dict) -> str:
    lines = ["# Независимый пересчёт D и A_link", "",
             f"Статус: `{result['status']}`.", "",
             "CPU, исходные файлы и все сохранённые пороги по неделям указаны в JSON рядом.",
             "Это проверка на ранее просмотренной истории. Достоверность временного состава",
             "готовых признаков и справочника требует проверки по исходным журналам.", ""]
    if result["historical_metrics"] is None:
        return "\n".join(lines+["Пересчёт не выполнен: отсутствуют входные данные.", ""])
    lines += ["| Голова / политика | Кандидаты | Рекомендации | Hits / unknown | Precision, нижняя граница | Recall канало-суток | Новые эпизоды / доступные | Повторы / продолжающиеся | В сутки |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    def percent(value):
        return "—" if value is None else f"{100*value:.2f}%"
    for head in result["historical_metrics"]:
        for name, row in head["total"].items():
            lines.append(f"| {head['head']} / {name} | {row['candidates']} | {row['alerts']} | "
                         f"{row['hits']} / {row['unknown_alerts']} | {percent(row['precision_lower_bound'])} | "
                         f"{percent(row['recall_known_days'])} | {row['episodes_caught']} / {row['episodes_eligible']} | "
                         f"{row['repeat_episode_alerts']} / {row['ongoing_episode_alerts']} | {row['alerts_per_day']:.2f} |")
    lines += ["", "Рекомендации: удалённая диагностика связи для A_link; плановая ручная",
              "диагностика оборудования для D. Решение о пилоте требует рассмотрения каждого",
              "временного окна и сравнения с одноимённой политикой baseline; скрипт не меняет",
              "статус голов и не разрешает автоматические заявки.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "historical"), default="preflight")
    parser.add_argument("--data-root", type=Path, default=ROOT/"data")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--test-days", type=int, default=90)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--refresh-days", type=int, default=7)
    parser.add_argument("--end", type=dt.date.fromisoformat,
                        help="последний день теста, например 2025-12-31 (по умолчанию — конец данных)")
    args = parser.parse_args()
    if min(args.splits, args.test_days, args.threads, args.refresh_days) < 1:
        parser.error("counts must be positive")
    output = args.output or ROOT/"reports"/f"second_ml_{args.mode}.json"
    result = (preflight(args.data_root) if args.mode == "preflight" else
              historical(args.data_root, n_splits=args.splits, test_days=args.test_days,
                         threads=args.threads, refresh_days=args.refresh_days,
                         end_limit=args.end))
    result["code_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    result["code_files_sha256"] = {name: sha256(ROOT/name) for name in (
        "scripts/audit_second_ml.py", "src/mkl/second_ml_audit.py", "configs/heads.yaml")}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                 allow_nan=False)+"\n", encoding="utf-8")
    if args.mode == "historical":
        output.with_suffix(".md").write_text(markdown_report(result), encoding="utf-8")
    print(f"{result['status']}: {output}")
    if args.mode == "historical" and result["status"] == "blocked_missing_data":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
