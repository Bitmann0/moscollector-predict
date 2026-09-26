"""Refresh deployed models from the latest *observable* journal day.

The fixed 2026 evaluation remains in final_eval.py. This script is for the
rolling service: a past 30-day block calibrates probabilities and the next
30 days select the alert threshold. The model sees only earlier days, with
the configured embargo between training and calibration.
"""
import argparse
import datetime as dt
import json
import math
import sys

import numpy as np
import polars as pl

from mkl import calibrate, config, db, labels, metrics, serve, store, train
from mkl.config import PATHS
from mkl.cv import Split, live_windows

sys.stdout.reconfigure(encoding="utf-8")


def refresh(head: str, cfg: dict, backend: str = "lgbm") -> dict:
    start = dt.date.fromisoformat(
        config.head_a_choice().get("window_start", "2023-01-01"))
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    source_last_day = con.execute("SELECT max(day) FROM daily_channel").fetchone()[0]
    labels.build_for_head(con, cfg)
    lab = con.execute(f"SELECT * FROM {cfg['label']} WHERE day >= ?", [start]).pl()
    con.close()
    if lab.is_empty():
        raise ValueError(f"{head}: нет наблюдаемых меток")
    feature_last_day = pl.scan_parquet(
        PATHS.features / f"{cfg['feature_set']}.parquet").select(
            pl.col("day").max()).collect().item()
    if feature_last_day < source_last_day:
        raise ValueError(
            f"{head}: feature store ends {feature_last_day}, "
            f"but source panel ends {source_last_day}; rebuild features first")
    dates = live_windows(lab["day"].max(), cfg["embargo_days"])
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
                  "feature_last_day": str(feature_last_day)})
    return {"head": head, "backend": backend, "artifact": str(artifact),
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
    args = parser.parse_args()
    configs = serve.load_heads()
    unknown = set(args.heads) - set(configs)
    if unknown:
        parser.error("неизвестные головы: " + ", ".join(sorted(unknown)))
    results = []
    selected = (args.heads or (list(configs) if args.all_heads else
                [h for h, cfg in configs.items()
                 if cfg.get("product_status") == "pilot"]))
    for head in selected:
        if configs[head].get("serving_rule"):
            # Голова-правило обученной модели не имеет: порог выбирается при
            # расчёте (src/mkl/rule_head.py, reports/RULE_VS_MODEL_RESULT.md).
            print(f"{head}: правило {configs[head]['serving_rule']}, модель не обучается",
                  flush=True)
            continue
        result = refresh(head, configs[head], backend=args.backend)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    report = PATHS.reports / "latest_model_refresh.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2,
                                 allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
