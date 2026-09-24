"""Refresh deployed models from the latest *observable* journal day.

The fixed 2026 evaluation remains in final_eval.py. This script is for the
rolling service: a past 30-day block calibrates probabilities and the next
30 days select the alert threshold. The model sees only earlier days, with
the configured embargo between training and calibration.
"""
import argparse
import datetime as dt
import json
import sys

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
    labels.build_for_head(con, cfg)
    lab = con.execute(f"SELECT * FROM {cfg['label']} WHERE day >= ?", [start]).pl()
    con.close()
    if lab.is_empty():
        raise ValueError(f"{head}: нет наблюдаемых меток")
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
                    horizon_days=cfg["horizon_days"])
    model = fit["model"]
    if model is None:
        raise ValueError(f"{head}: нет позитивов в обучении или валидации")
    keys = [key for key in train.KEYS if key in feats.columns and key in lab.columns]
    valid = feats.join(lab, on=keys, how="inner")
    cal = valid.filter((pl.col("day") >= dates["calibration_start"]) &
                       (pl.col("day") <= dates["calibration_end"]))
    threshold = valid.filter((pl.col("day") >= dates["threshold_start"]) &
                             (pl.col("day") <= dates["threshold_end"]))
    if cal.is_empty() or threshold.is_empty() or cal["y"].sum() == 0:
        raise ValueError(f"{head}: недостаточно данных для калибровки")
    names = fit["feature_names"]
    iso = calibrate.fit_isotonic(
        model.predict_proba(train._matrix(cal, names))[:, 1], cal["y"].to_numpy())
    p = calibrate.apply(
        iso, model.predict_proba(train._matrix(threshold, names))[:, 1])
    pick = metrics.target_operating_point(threshold["y"].to_numpy(), p,
                                          min_precision=0.7)
    selected_threshold = pick.get("threshold") if pick.get("feasible") else 1.0
    artifact = serve.save(head, model, iso, names, threshold=selected_threshold)
    return {"head": head, "backend": backend, "artifact": str(artifact),
            "last_observable_day": str(dates["threshold_end"]),
            **{key: str(value) for key, value in dates.items()},
            "threshold": selected_threshold,
            "threshold_feasible": bool(pick.get("feasible")),
            "validation_pr_auc": fit["mean"].get("pr_auc"),
            "validation_daily_precision": fit["mean"].get("daily_precision_at_k"),
            "validation_daily_recall": fit["mean"].get("daily_recall_at_k")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("heads", nargs="*", help="имена голов; пусто = все")
    parser.add_argument("--backend", choices=("lgbm", "xgb", "cat"), default="lgbm")
    args = parser.parse_args()
    configs = serve.load_heads()
    unknown = set(args.heads) - set(configs)
    if unknown:
        parser.error("неизвестные головы: " + ", ".join(sorted(unknown)))
    results = []
    for head in (args.heads or list(configs)):
        result = refresh(head, configs[head], backend=args.backend)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    report = PATHS.reports / "latest_model_refresh.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
