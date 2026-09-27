"""Refresh deployed models from the latest *observable* journal day.

The fixed 2026 evaluation remains in final_eval.py. This script is for the
rolling service: a past 30-day block calibrates probabilities and the next
30 days select the alert threshold. The model sees only earlier days, with
the configured embargo between training and calibration.

--source-end C trains the same way on the data as of the end of day C and
writes models/{head}@{threshold_end}.pkl beside models/{head}.pkl. The service
picks a dated artifact by asof (serve.artifact_path), so a historical demo day
gets a model whose threshold window ended before it. --window-plan prints the
fewest source-end dates that cover a demo window within max_model_lag_days.
"""
import argparse
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import polars as pl

from mkl import calibrate, config, cv, db, labels, metrics, serve, store, train
from mkl.config import PATHS
from mkl.cv import Split, live_windows

DEMO_WINDOW = (dt.date(2026, 6, 1), dt.date(2026, 6, 30))

sys.stdout.reconfigure(encoding="utf-8")


def output_path(out: Path | None, head: str, threshold_end: dt.date,
                dated: bool) -> Path:
    """Файл артефакта: явный .pkl как есть, каталог или отсечка — {head}@{threshold_end}.pkl."""
    if out is not None and out.suffix == ".pkl":
        return out
    if out is None:
        return serve.model_path(head, threshold_end if dated else None)
    return out / serve.model_path(head, threshold_end).name


def window_plan(head: str, cfg: dict, first: dt.date, last: dt.date) -> list[dict]:
    return cv.window_plan(first, last, int(cfg["horizon_days"]),
                          int(cfg["embargo_days"]), serve.max_lag_days(cfg))


def refresh(head: str, cfg: dict, backend: str = "lgbm",
            source_end: dt.date | None = None, out: Path | None = None) -> dict:
    start = dt.date.fromisoformat(
        config.head_a_choice().get("window_start", "2023-01-01"))
    con = db.connect()
    # Обрезка до меток: иначе L9c цензурировал бы последние строки каналов по
    # полной панели, а порог выбирался бы по исходам, неизвестным к source_end.
    db.attach_label_sources(con, source_end)
    source_last_day = con.execute("SELECT max(day) FROM daily_channel").fetchone()[0]
    if source_end is not None and source_last_day != source_end:
        con.close()
        raise ValueError(f"{head}: панель кончается {source_last_day}, "
                         f"а --source-end {source_end}")
    labels.build_for_head(con, cfg)
    lab = con.execute(f"SELECT * FROM {cfg['label']} WHERE day >= ?", [start]).pl()
    con.close()
    if lab.is_empty():
        raise ValueError(f"{head}: нет наблюдаемых меток")
    feature_last_day = pl.scan_parquet(
        PATHS.features / f"{cfg['feature_set']}.parquet").select(
            pl.col("day").max()).collect().item()
    if source_end is not None:
        feature_last_day = min(feature_last_day, source_end)
    if feature_last_day < source_last_day:
        raise ValueError(
            f"{head}: feature store ends {feature_last_day}, "
            f"but source panel ends {source_last_day}; rebuild features first")
    dates = live_windows(lab["day"].max(), cfg["embargo_days"])
    if source_end is not None:
        planned = cv.live_threshold_end(source_end, int(cfg["horizon_days"]),
                                        int(cfg["embargo_days"]))
        if dates["threshold_end"] != planned:
            # План окна (--window-plan) рассчитывал на planned; покрытие дней
            # демо тогда надо проверить заново по фактическому концу окна.
            print(f"{head}: окно порога кончается {dates['threshold_end']}, "
                  f"план ждал {planned}", file=sys.stderr, flush=True)
    # Срез фичестора кончается на threshold_end, то есть не позже последнего
    # дня метки и раньше source_end.
    feats = store.read_slice(cfg["feature_set"], start, dates["threshold_end"])
    drop = cfg.get("drop_feature_prefixes") or []
    if drop:
        feats = feats.select([c for c in feats.columns
                              if not any(c.startswith(prefix) for prefix in drop)])
    split = Split(start, dates["training_end"],
                  dates["calibration_start"], dates["threshold_end"])
    fit = train.run(head, feats, lab, [split],
                    params=train.params_for(cfg, backend),
                    budget_per_day=cfg["budget_per_day"], backend=backend,
                    horizon_days=cfg["horizon_days"],
                    budget_per_object=bool(cfg.get("budget_per_object")))
    model = fit["model"]
    if model is None:
        raise ValueError(f"{head}: нет позитивов в обучении или валидации")
    keys = [key for key in train.KEYS if key in feats.columns and key in lab.columns]
    # A_link's final report for each channel has an unresolved outcome. It
    # still competes for the live daily budget; dropping it before threshold
    # selection would tune on a smaller, easier candidate set than serving.
    unknown_in_budget = bool(cfg.get("unknown_in_budget"))
    valid = feats.join(lab, on=keys,
                       how="left" if unknown_in_budget else "inner")
    cal = valid.filter((pl.col("day") >= dates["calibration_start"]) &
                       (pl.col("day") <= dates["calibration_end"]) &
                       pl.col("y").is_not_null())
    threshold = valid.filter((pl.col("day") >= dates["threshold_start"]) &
                             (pl.col("day") <= dates["threshold_end"]))
    # Stable tie order must match serve._apply_budget: isotonic calibration
    # often assigns identical probabilities to dozens of entities.
    tie = [key for key in ("obj", "ch", "seg") if key in threshold.columns]
    threshold = threshold.sort(["day"] + tie)
    if cal.is_empty() or threshold.is_empty() or cal["y"].sum() == 0:
        raise ValueError(f"{head}: недостаточно данных для калибровки")
    names = fit["feature_names"]
    iso = calibrate.fit_isotonic(
        model.predict_proba(train._matrix(cal, names))[:, 1], cal["y"].to_numpy())
    p = calibrate.apply(
        iso, model.predict_proba(train._matrix(threshold, names))[:, 1])
    objects = (threshold["obj"].to_numpy()
               if cfg.get("budget_per_object") else None)
    threshold_y = threshold["y"].fill_null(0).to_numpy()
    pick = metrics.daily_target_operating_point(
        threshold_y, p, threshold["day"].to_numpy(),
        cfg["budget_per_day"],
        min_precision=float(cfg.get("operating_min_precision", 0.7)),
        objects=objects,
        min_alerts=int(cfg.get("operating_min_alerts", 30)))
    # Calibrated probabilities are <=1. A finite value just above one
    # guarantees no alert when no precision-eligible threshold exists.
    selected_threshold = (pick["threshold"] if pick.get("feasible")
                          else float(np.nextafter(1.0, np.inf)))
    actual = metrics.daily_budget_summary(
        threshold_y, p, threshold["day"].to_numpy(),
        cfg["budget_per_day"], threshold=selected_threshold,
        objects=objects)
    artifact = serve.save(
        head, model, iso, names, threshold=selected_threshold,
        path=output_path(out, head, dates["threshold_end"], source_end is not None),
        metadata={"head": head, "label": cfg["label"],
                  "variant": cfg.get("variant"),
                  "horizon_days": cfg["horizon_days"],
                  "operating_min_precision": float(
                      cfg.get("operating_min_precision", 0.7)),
                  "unknown_in_budget": unknown_in_budget,
                  "training_end": str(dates["training_end"]),
                  "calibration_end": str(dates["calibration_end"]),
                  "threshold_end": str(dates["threshold_end"]),
                  "source_last_day": str(source_last_day),
                  "source_end": str(source_end) if source_end else None,
                  "feature_last_day": str(feature_last_day)})
    return {"head": head, "backend": backend, "artifact": str(artifact),
            "source_end": str(source_end) if source_end else None,
            "last_observable_day": str(dates["threshold_end"]),
            **{key: str(value) for key, value in dates.items()},
            "threshold": selected_threshold,
            "threshold_feasible": bool(pick.get("feasible")),
            "threshold_unknown_candidates": threshold["y"].null_count(),
            "operating_min_precision": float(
                cfg.get("operating_min_precision", 0.7)),
            "threshold_daily_precision": (actual["daily_precision_at_k"]
                                          if math.isfinite(actual["daily_precision_at_k"])
                                          else None),
            "threshold_daily_recall": actual["daily_recall_at_k"],
            "threshold_daily_alerts": actual["daily_alerts"],
            "validation_pr_auc": fit["mean"].get("pr_auc"),
            "validation_daily_precision": fit["mean"].get("daily_precision_at_k"),
            "validation_daily_recall": fit["mean"].get("daily_recall_at_k")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("heads", nargs="*", help="имена голов; пусто = pilot")
    parser.add_argument("--all-heads", action="store_true",
                        help="исследовательское переобучение всех голов")
    parser.add_argument("--backend", choices=("lgbm", "xgb", "cat"), default="lgbm")
    parser.add_argument("--source-end", type=dt.date.fromisoformat,
                        help="обучить по данным на конец суток YYYY-MM-DD; "
                             "артефакт models/{head}@{threshold_end}.pkl")
    parser.add_argument("--out", type=Path,
                        help="файл .pkl или каталог; в каталог пишется "
                             "{head}@{threshold_end}.pkl")
    parser.add_argument("--window-plan", action="store_true",
                        help="напечатать отсечки --source-end для окна демо и выйти")
    parser.add_argument("--train-window", action="store_true",
                        help="обучить все артефакты плана окна демо")
    parser.add_argument("--window-start", type=dt.date.fromisoformat,
                        default=DEMO_WINDOW[0], help="первый день окна демо")
    parser.add_argument("--window-end", type=dt.date.fromisoformat,
                        default=DEMO_WINDOW[1], help="последний день окна демо")
    args = parser.parse_args()
    configs = serve.load_heads()
    unknown = set(args.heads) - set(configs)
    if unknown:
        parser.error("неизвестные головы: " + ", ".join(sorted(unknown)))
    if args.train_window and args.source_end:
        parser.error("--train-window сам задаёт отсечки; --source-end с ним не нужен")
    selected = (args.heads or (list(configs) if args.all_heads else
                [h for h, cfg in configs.items()
                 if cfg.get("product_status") == "pilot"]))
    runs: list[tuple[str, dt.date | None]] = []
    for head in selected:
        if configs[head].get("serving_rule"):
            # Голова-правило обученной модели не имеет: порог выбирается при
            # расчёте (src/mkl/rule_head.py, reports/RULE_VS_MODEL_RESULT.md).
            print(f"{head}: правило {configs[head]['serving_rule']}, модель не обучается",
                  flush=True)
            continue
        if args.window_plan or args.train_window:
            plan = window_plan(head, configs[head], args.window_start, args.window_end)
            if args.window_plan:
                print(json.dumps({
                    "head": head,
                    "window": [str(args.window_start), str(args.window_end)],
                    "max_model_lag_days": serve.max_lag_days(configs[head]),
                    "artifacts": [{key: str(value) for key, value in item.items()}
                                  for item in plan]}, ensure_ascii=False), flush=True)
                continue
            runs.extend((head, item["source_end"]) for item in plan)
        else:
            runs.append((head, args.source_end))
    if args.window_plan:
        return
    if args.out is not None and args.out.suffix == ".pkl" and len(runs) > 1:
        parser.error("--out с файлом .pkl годится для одного артефакта; укажите каталог")
    reports: dict[str, list] = defaultdict(list)
    if not runs:
        _write_report("latest_model_refresh.json", [])
    for head, source_end in runs:
        result = refresh(head, configs[head], backend=args.backend,
                         source_end=source_end, out=args.out)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        # Датированный прогон не подменяет отчёт о последнем переобучении
        # сервиса. Отчёт пишется после каждого прогона: при сбое на третьей
        # отсечке первые две остаются описанными.
        name = (f"model_refresh@{source_end}.json" if source_end
                else "latest_model_refresh.json")
        reports[name].append(result)
        _write_report(name, reports[name])


def _write_report(name: str, items: list) -> None:
    (PATHS.reports / name).write_text(
        json.dumps(items, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")


if __name__ == "__main__":
    main()
