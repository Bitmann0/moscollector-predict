"""Чувствительность A_link к цензуре метки L9c: провалы выгрузки, возврат, выходные.

L9c считает началом разрыва любой пропуск суток длиннее полутора обычных
промежутков канала. Такой пропуск бывает и без неисправности, и PR #9
цензурировал два случая из трёх, проверяемых здесь:

- coverage_weekday и coverage_30d_pr9 — окно метки задевает сутки, когда
  журнал пришёл от малой доли каналов. Второй вариант — правило PR #9 как
  есть, первый — с поправкой на день недели (label_censoring.coverage_calendar);
- recovery_30d — канал молчал дольше 30 суток: без возврата в журнал
  временный пропуск не отличить от списания (широкая метка PR #9);
- no_friday_monday — пятница, после которой канал вернулся в понедельник,
  то есть предсказуемое молчание по выходным. Этого правила в PR #9 нет.

--labels-only строит только метки: сколько строк и позитивов снимает каждое
правило и на какие дни недели приходятся провалы выгрузки. Это DuckDB по
суточной панели, без признаков и обучения.

Полный режим повторяет цикл eval_a_link_policy.py для каждого варианта: те же
пять 90-дневных тестовых окон, калибровка и порог на двух предыдущих
30-дневных окнах, LightGBM с параметрами heads.yaml, не больше 20 рекомендаций
в сутки и пауза 7 суток. Строки, снятые цензурой, остаются в ранжировании с
исходом unknown, как в живом сервисе. Для каждого варианта отдельно считается,
сколько рекомендаций модели, обученной на L9c, попали в его цензурированные
строки и сколько из них были попаданиями: это ответ на вопрос, держится ли
точность A_link на таких пропусках, без переобучения.

Продуктовую метку, heads.yaml и a_link_live_policy_temporal.json скрипт не
меняет.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path

import polars as pl

from mkl import calibrate, cv, db, label_censoring, labels, serve, store, train
try:
    from scripts.eval_a_link_policy import (BASELINE_FEATURE, COOLDOWN_DAYS,
                                            N_SPLITS, TEST_DAYS, WINDOW_DAYS,
                                            _select, _summary_unknown,
                                            _threshold)
    from scripts.eval_d_live_policy import _period, _scored
except ModuleNotFoundError:  # direct: python scripts/eval_a_link_availability_checks.py
    from eval_a_link_policy import (BASELINE_FEATURE, COOLDOWN_DAYS, N_SPLITS,
                                    TEST_DAYS, WINDOW_DAYS, _select,
                                    _summary_unknown, _threshold)
    from eval_d_live_policy import _period, _scored

sys.stdout.reconfigure(encoding="utf-8")

# Начало выборки то же, что в eval_a_link_policy.py: иначе окна walk_forward
# сдвинутся и сравнивать варианты с продуктовым отчётом будет не с чем.
START = dt.date(2023, 1, 1)
RECOVERY_DAYS = 30
BASE = "L9c"
KEYS = ["ch", "day"]
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

VARIANTS = {
    BASE: ("продуктовая метка A_link без дополнительной цензуры", None),
    "coverage_weekday": (
        "без строк, чьё окно задевает сутки с числом каналов меньше 50% "
        "медианы того же дня недели за 8 предыдущих недель",
        lambda con, t, h: label_censoring.censor_low_coverage(con, t, h, by_weekday=True)),
    "coverage_30d_pr9": (
        "правило PR #9: без строк, чьё окно задевает сутки с числом каналов "
        "меньше 50% медианы за 30 предыдущих суток",
        lambda con, t, h: label_censoring.censor_low_coverage(con, t, h, by_weekday=False)),
    "recovery_30d": (
        f"без строк, после которых канал молчал дольше {RECOVERY_DAYS} суток",
        lambda con, t, h: label_censoring.censor_unrecovered(con, t, RECOVERY_DAYS)),
    "no_friday_monday": (
        "без пятничных строк, после которых канал вернулся в понедельник",
        lambda con, t, h: label_censoring.censor_weekend_gap(con, t)),
}


def build_labels(con, cfg: dict, names: list[str], start: dt.date,
                 end: dt.date) -> dict[str, pl.DataFrame]:
    """Метка A_link и её варианты за [start, end]; L9c всегда первой."""
    table = labels.build_for_head(con, cfg)
    out = {}
    for name in [BASE] + [n for n in names if n != BASE]:
        censor = VARIANTS[name][1]
        con.execute(f"CREATE OR REPLACE TEMP TABLE _variant AS SELECT * FROM {table}")
        if censor is not None:
            censor(con, "_variant", cfg["horizon_days"])
        out[name] = con.execute(
            "SELECT ch, day, y FROM _variant WHERE day >= ? AND day <= ? "
            "ORDER BY day, ch", [start, end]).pl()
    return out


def _in_windows(frame: pl.DataFrame, splits: list[cv.Split]) -> pl.DataFrame:
    cond = pl.lit(False)
    for s in splits:
        cond = cond | pl.col("day").is_between(s.test_start, s.test_end)
    return frame.filter(cond)


def _by_weekday(frame: pl.DataFrame, shift_days: int = 0) -> dict[str, int]:
    """Счёт строк по дню недели суток day + shift_days."""
    wd = (pl.col("day") + pl.duration(days=shift_days)).dt.weekday()
    counts = dict(frame.group_by(wd.alias("wd")).len().iter_rows())
    return {WEEKDAYS[i - 1]: int(counts.get(i, 0)) for i in range(1, 8)}


def _label_counts(base: pl.DataFrame, lab: pl.DataFrame, horizon_days: int) -> dict:
    censored = base.join(lab.select(KEYS), on=KEYS, how="anti")
    pos = lab.filter(pl.col("y") == 1)
    return {"rows": lab.height, "positives": pos.height,
            "positive_rate": pos.height / lab.height if lab.height else None,
            "censored_rows": censored.height,
            "censored_positives": censored.filter(pl.col("y") == 1).height,
            # День недели первых пропущенных суток, а не строки: при горизонте
            # в сутки это day + 1.
            "censored_positives_by_first_silent_weekday": _by_weekday(
                censored.filter(pl.col("y") == 1), horizon_days)}


def coverage_audit(con, start: dt.date) -> dict:
    """Провалы выгрузки по обоим правилам: сколько, по каким дням недели."""
    out = {}
    for rule, by_weekday in (("weekday_8w", True), ("trailing_30d_pr9", False)):
        label_censoring.coverage_calendar(con, by_weekday=by_weekday, table="_cal")
        cal = con.execute("SELECT * FROM _cal ORDER BY day").pl()
        periods = {}
        for key, lo in (("all", cal["day"].min()), ("since_start", start)):
            sub = cal.filter(pl.col("day") >= lo)
            low = sub.filter(pl.col("low"))
            periods[key] = {"from": str(lo), "to": str(sub["day"].max()),
                            "calendar_days": sub.height, "low_days": low.height,
                            "low_days_by_weekday": _by_weekday(low)}
        out[rule] = periods
        if by_weekday:
            since = cal.filter(pl.col("day") >= start)
            out[rule]["since_start"]["low_dates"] = [
                str(d) for d in since.filter(pl.col("low"))["day"]]
            out["zero_channel_dates"] = [
                str(d) for d in cal.filter(pl.col("n_channels") == 0)["day"]]
            med = dict(since.group_by(pl.col("day").dt.weekday().alias("wd"))
                       .agg(pl.col("n_channels").median()).iter_rows())
            out["median_channels_by_weekday_since_start"] = {
                WEEKDAYS[i - 1]: med.get(i) for i in range(1, 8)}
    return out


def label_audit(con, labs: dict[str, pl.DataFrame], splits: list[cv.Split],
                horizon_days: int, start: dt.date) -> dict:
    """Сколько строк и позитивов снимает каждое правило, без обучения."""
    base = labs[BASE]
    base_windows = _in_windows(base, splits)
    variants = {}
    for name, lab in labs.items():
        variants[name] = {
            "description": VARIANTS[name][0],
            "span": _label_counts(base, lab, horizon_days),
            "test_windows": _label_counts(base_windows, _in_windows(lab, splits),
                                          horizon_days)}
    windows_pos = base_windows.filter(pl.col("y") == 1)
    return {"span": [str(base["day"].min()), str(base["day"].max())],
            "test_windows": [[str(s.test_start), str(s.test_end)] for s in splits],
            "l9c_test_window_positives_by_first_silent_weekday": _by_weekday(
                windows_pos, horizon_days),
            "variants": variants,
            "coverage": coverage_audit(con, start)}


def _censor_rows(data: pl.DataFrame, base: pl.DataFrame,
                 lab: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Данные L9c, где снятые вариантом строки получили исход unknown.

    Признаки не копируются: меняется одна колонка y. Иначе на каждый вариант
    ушла бы ещё одна копия фичестора.
    """
    censored = base.join(lab.select(KEYS), on=KEYS, how="anti")
    idx = (data.select(KEYS).with_row_index("_i")
           .join(censored.select(KEYS), on=KEYS, how="semi")["_i"]
           .cast(pl.Int64))
    y = (pl.when(pl.int_range(pl.len(), dtype=pl.Int64).is_in(idx.implode()))
         .then(None).otherwise(pl.col("y")).alias("y"))
    return data.with_columns(y), censored


def _chosen(scored: pl.DataFrame, threshold: float | None, cfg: dict) -> pl.DataFrame:
    served = serve.alerts_over_time(
        scored, cfg["budget_per_day"], entity="ch",
        cooldown_days=COOLDOWN_DAYS, threshold=threshold)
    return served.filter(pl.col("alert")).select("ch", "day", "y")


def _fold(cfg: dict, feats: pl.DataFrame, lab: pl.DataFrame, data: pl.DataFrame,
          split: cv.Split, start: dt.date) -> tuple[dict, pl.DataFrame]:
    """Один тестовый блок eval_a_link_policy.py на заданной метке.

    Возвращает строку отчёта в полях a_link_live_policy_temporal.json и
    рекомендации модели по продуктовой политике.
    """
    minimum = float(cfg["operating_min_precision"])
    policy = f"min_precision_{minimum:.2f}"
    threshold_end = split.test_start - dt.timedelta(days=cfg["horizon_days"] + 1)
    threshold_start = threshold_end - dt.timedelta(days=WINDOW_DAYS - 1)
    calibration_end = threshold_start - dt.timedelta(days=1)
    calibration_start = calibration_end - dt.timedelta(days=WINDOW_DAYS - 1)
    training_end = calibration_start - dt.timedelta(days=cfg["embargo_days"] + 1)
    fit = train.run(
        "A_link", feats, lab,
        [cv.Split(start, training_end, calibration_start, calibration_end)],
        params=train.params_for(cfg, "lgbm"), backend="lgbm",
        budget_per_day=cfg["budget_per_day"], horizon_days=cfg["horizon_days"])
    model, names = fit["model"], fit["feature_names"]
    if model is None:
        raise ValueError("A_link has no train/calibration positives")
    cal = _period(data, calibration_start, calibration_end).filter(
        pl.col("y").is_not_null())
    thr = _period(data, threshold_start, threshold_end)
    test = _period(data, split.test_start, split.test_end)
    iso = calibrate.fit_isotonic(
        model.predict_proba(train._matrix(cal, names))[:, 1], cal["y"].to_numpy())
    threshold_scores = _scored(thr, model, iso, names)
    test_scores = _scored(test, model, iso, names)
    rule_threshold_scores = _scored(thr, model, iso, names, BASELINE_FEATURE)
    rule_test_scores = _scored(test, model, iso, names, BASELINE_FEATURE)
    model_pick = _select(threshold_scores, cfg, minimum)
    rule_pick = _select(rule_threshold_scores, cfg, minimum)
    model_threshold = _threshold(model_pick, threshold_scores)
    policies = {
        policy: {
            "selection": model_pick,
            "model": _summary_unknown(test_scores, model_threshold, cfg),
            "baseline_selection": rule_pick,
            "baseline": _summary_unknown(
                rule_test_scores, _threshold(rule_pick, rule_threshold_scores), cfg),
        },
        "fixed_top_20": {
            "model": _summary_unknown(test_scores, None, cfg),
            "baseline": _summary_unknown(rule_test_scores, None, cfg)},
    }
    unknown = test["y"].null_count()
    row = {"test_start": str(split.test_start), "test_end": str(split.test_end),
           "training_end": str(training_end),
           "calibration": [str(calibration_start), str(calibration_end)],
           "threshold_window": [str(threshold_start), str(threshold_end)],
           "known_rows": test.height - unknown, "unknown_rows": unknown,
           "policies": policies}
    return row, _chosen(test_scores, model_threshold, cfg)


def _totals(rows: list[dict]) -> dict:
    out = {}
    for key in rows[0]["policies"]:
        for who in ("model", "baseline"):
            parts = [r["policies"][key][who] for r in rows]
            alerts = sum(p["alerts"] for p in parts)
            hits = sum(p["hits"] for p in parts)
            unknown = sum(p["unknown_alerts"] for p in parts)
            caught = sum(p["episodes_caught"] for p in parts)
            episodes = sum(p["episodes"] for p in parts)
            out.setdefault(key, {})[who] = {
                "alerts": alerts, "hits": hits, "unknown_alerts": unknown,
                "precision_lower_bound": hits / alerts if alerts else None,
                "precision_known_only": (hits / (alerts - unknown)
                                         if alerts > unknown else None),
                "episodes": episodes, "episodes_caught": caught}
    overlap = [r["l9c_recommendations_censored"] for r in rows
               if "l9c_recommendations_censored" in r]
    if overlap:
        out["l9c_recommendations_censored"] = {
            k: sum(o[k] for o in overlap) for k in overlap[0]}
    return out


def evaluate(cfg: dict, feats: pl.DataFrame, labs: dict[str, pl.DataFrame],
             splits: list[cv.Split], start: dt.date) -> dict:
    """Цикл eval_a_link_policy.py для каждого варианта метки."""
    keys = [k for k in ("ch", "obj", "day")
            if k in feats.columns and k in labs[BASE].columns]
    # Левое соединение, как в eval_a_link_policy.py: канал с неизвестным
    # исходом в живом сервисе всё равно занимает место в лимите.
    data = feats.join(labs[BASE], on=keys, how="left").sort(["day", "obj", "ch"])
    if BASELINE_FEATURE not in data.columns:
        raise ValueError(f"missing baseline feature {BASELINE_FEATURE}")
    policy = f"min_precision_{float(cfg['operating_min_precision']):.2f}"
    chosen_base: list[pl.DataFrame] = []
    out = {}
    for name, lab in labs.items():
        if name == BASE:
            variant_data, censored = data, None
        else:
            variant_data, censored = _censor_rows(data, labs[BASE], lab)
        rows = []
        for i, split in enumerate(splits):
            row, chosen = _fold(cfg, feats, lab, variant_data, split, start)
            if censored is None:
                chosen_base.append(chosen)
            else:
                hit = chosen_base[i].join(censored.select(KEYS), on=KEYS, how="semi")
                row["l9c_recommendations_censored"] = {
                    "recommendations": hit.height,
                    "hits": hit.filter(pl.col("y") == 1).height}
            rows.append(row)
            model = row["policies"][policy]["model"]
            print(json.dumps({"variant": name, "test_start": row["test_start"],
                              "alerts": model["alerts"], "hits": model["hits"],
                              "unknown_alerts": model["unknown_alerts"],
                              "censored": row.get("l9c_recommendations_censored")},
                             ensure_ascii=False), flush=True)
        out[name] = {"description": VARIANTS[name][0], "folds": rows,
                     "totals": _totals(rows)}
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _finite(obj):
    """NaN из метрик эпизодов -> None: json с allow_nan=False их не пропустит."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_finite(v) for v in obj]
    return obj


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--end-date", type=dt.date.fromisoformat,
                        help="last mature label day; default latest available")
    parser.add_argument("--labels-only", action="store_true",
                        help="only label and coverage counts, no features or training")
    parser.add_argument("--variants", default=",".join(VARIANTS),
                        help=f"comma-separated subset of {', '.join(VARIANTS)}")
    parser.add_argument("--out", type=Path, help="output JSON path")
    parser.add_argument("--memory-limit", default="10GB", help="DuckDB memory limit")
    parser.add_argument("--threads", type=int, default=8, help="DuckDB threads")
    args = parser.parse_args()
    names = [n.strip() for n in args.variants.split(",") if n.strip()]
    unknown_names = sorted(set(names) - set(VARIANTS))
    if unknown_names:
        parser.error(f"unknown variants: {unknown_names}")
    cfg = serve.load_heads()["A_link"]
    panel = store.PATHS.interim / "daily_channel.parquet"
    con = db.connect(memory_limit=args.memory_limit, threads=args.threads)
    # L9c читает только суточную панель: эпизоды и групповые отказы ей не нужны.
    db.attach_parquet(con, "daily_channel")
    labs = build_labels(con, cfg, names, START, args.end_date or dt.date.max)
    splits = cv.walk_forward(sorted(labs[BASE]["day"].unique().to_list()),
                             N_SPLITS, TEST_DAYS, cfg["embargo_days"])
    audit = label_audit(con, labs, splits, cfg["horizon_days"], START)
    rows, first, last = con.execute(
        "SELECT count(*), min(day), max(day) FROM daily_channel").fetchone()
    con.close()
    result = {"head": "A_link", "label_variant": cfg["variant"],
              "horizon_days": cfg["horizon_days"],
              "evaluated_through": str(labs[BASE]["day"].max()),
              "input": {"file": panel.name, "sha256": _sha256(panel),
                        "rows": rows, "days": [str(first), str(last)]},
              "label_audit": audit}
    if args.labels_only:
        path = args.out or store.PATHS.reports / "a_link_label_censoring_audit.json"
    else:
        feats = store.read_slice(cfg["feature_set"], START, labs[BASE]["day"].max())
        drop = cfg.get("drop_feature_prefixes") or []
        if drop:
            feats = feats.select([c for c in feats.columns
                                  if not any(c.startswith(p) for p in drop)])
        result["policy"] = (f"daily_top_{cfg['budget_per_day']}; "
                            f"calibrated_prior_threshold_min_precision_"
                            f"{float(cfg['operating_min_precision']):.2f}; "
                            f"cooldown_{COOLDOWN_DAYS}d_no_backfill")
        result["baseline"] = BASELINE_FEATURE
        result["variants"] = evaluate(cfg, feats, labs, splits, START)
        path = args.out or store.PATHS.reports / "a_link_availability_checks.json"
    result["caution"] = (
        "Validation, not a blind holdout: the 2026H1 test windows were already "
        "inspected (reports/holdout_uses.md). Censored rows stay in the ranking "
        "as unknown outcomes. The product label, heads.yaml and "
        "a_link_live_policy_temporal.json are unchanged.")
    path.write_text(json.dumps(_finite(result), ensure_ascii=False, indent=2,
                               allow_nan=False), encoding="utf-8")
    print(f"saved {path}", flush=True)


if __name__ == "__main__":
    main()
