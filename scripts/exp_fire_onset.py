"""Compare all-state and quiet-week models on first fire alarms.

The outcome is still an SМВУ alarm proxy, not a confirmed fire. Both models
are evaluated on exactly the same quiet-week rows and 2025 walk-forward folds.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl

from mkl import cv, db, labels, metrics, serve, store, train
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")


def _score(model, names, frame: pl.DataFrame, budget: int) -> dict:
    y = frame["y"].to_numpy()
    p = model.predict_proba(train._matrix(frame, names))[:, 1]
    got = metrics.daily_budget_summary(y, p, frame["day"].to_numpy(), budget)
    return {"n": len(y), "positives": int(y.sum()),
            "base_rate": float(y.mean()), "pr_auc": metrics.pr_auc(y, p),
            "roc_auc": metrics.roc_auc(y, p),
            "daily_precision": got["daily_precision_at_k"],
            "daily_recall": got["daily_recall_at_k"],
            "alerts": got["daily_alerts"]}


def main() -> None:
    cfg = serve.load_heads()["B"]
    start, end = dt.date(2023, 1, 1), dt.date(2025, 12, 31)
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_fire(con)
    lab = con.execute("SELECT * FROM label_fire WHERE day BETWEEN ? AND ?",
                      [start, end]).pl()
    con.close()
    feat = store.read_slice("segment", start, end)
    feat = feat.select([c for c in feat.columns if not c.startswith("sensor_")])
    # days_since_fire includes today's observed alarm. A value >=7 means
    # no fire alarm in the current and previous six calendar days.
    quiet = feat.filter(pl.col("days_since_fire").is_null() |
                        (pl.col("days_since_fire") >= 7))
    quiet_labeled = quiet.join(lab, on=["obj", "seg", "day"]).sort(
        ["day", "obj", "seg"])
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()),
                             n_splits=3, test_days=90,
                             embargo_days=cfg["embargo_days"])
    results = {"definition": "alarm tomorrow after >=7 calendar days without an alarm",
               "folds": []}
    for split in splits:
        block = quiet_labeled.filter(
            (pl.col("day") >= split.test_start) &
            (pl.col("day") <= split.test_end))
        if block.is_empty() or block["y"].sum() == 0:
            continue
        row = {"start": str(split.test_start), "end": str(split.test_end)}
        for name, frame in (("all_state", feat), ("quiet_week", quiet)):
            fit = train.run("B", frame, lab, [split],
                            params=train.params_for(cfg, "lgbm"),
                            budget_per_day=cfg["budget_per_day"],
                            backend="lgbm")
            if fit["model"] is None:
                raise ValueError(f"{name}: no model on {split.test_start}")
            row[name] = _score(fit["model"], fit["feature_names"],
                               block, cfg["budget_per_day"])
        for name, col in (("historical_rate", "fire_rate_to_date"),
                          ("weekly_alarms", "n_alarms_w7")):
            y = block["y"].to_numpy()
            p = block[col].fill_null(0).to_numpy()
            got = metrics.daily_budget_summary(
                y, p, block["day"].to_numpy(), cfg["budget_per_day"])
            row[name] = {"pr_auc": metrics.pr_auc(y, p),
                         "daily_precision": got["daily_precision_at_k"],
                         "daily_recall": got["daily_recall_at_k"]}
        results["folds"].append(row)
        print(json.dumps(row, ensure_ascii=False, allow_nan=False), flush=True)
    if not results["folds"]:
        raise ValueError("no positive quiet-week test folds")
    results["mean"] = {
        name: {key: float(np.mean([f[name][key] for f in results["folds"]]))
               for key in ("pr_auc", "daily_precision", "daily_recall")}
        for name in ("all_state", "quiet_week", "historical_rate", "weekly_alarms")}
    path = PATHS.reports / "fire_onset_experiment.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2,
                               allow_nan=False), encoding="utf-8")
    print(json.dumps(results["mean"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
