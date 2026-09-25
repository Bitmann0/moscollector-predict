"""Compare fire model against rules using exactly the same 2025 folds."""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl

from mkl import cv, db, labels, metrics, serve, store
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    start, end = dt.date(2023, 1, 1), dt.date(2025, 12, 31)
    cfg = serve.load_heads()["B"]
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_fire(con)
    lab = con.execute("SELECT * FROM label_fire WHERE day BETWEEN ? AND ?",
                      [start, end]).pl()
    con.close()
    feat = store.read_slice("segment", start, end)
    data = feat.join(lab, on=["obj", "seg", "day"]).sort(["obj", "seg", "day"])
    # Only past labels: this baseline is useful for retrospective comparison,
    # while production would derive the same counts from observed n_fire.
    data = data.with_columns([
        pl.col("y").cum_sum().over(["obj", "seg"]).alias("_cum"),
        pl.int_range(pl.len()).over(["obj", "seg"]).alias("_seen"),
    ]).with_columns(((pl.col("_cum") - pl.col("y")) /
                     (pl.col("_seen") + 1)).alias("historical_rate"))
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=90,
                             embargo_days=cfg["embargo_days"])
    results = {}
    for name in ("n_fire", "n_alarms_w7", "historical_rate"):
        rows = []
        for split in splits:
            block = data.filter((pl.col("day") >= split.test_start) &
                                (pl.col("day") <= split.test_end))
            y = block["y"].to_numpy()
            p = block[name].fill_null(0).to_numpy()
            day = block["day"].to_numpy()
            res = metrics.daily_budget_summary(y, p, day, cfg["budget_per_day"])
            res["pr_auc"] = metrics.pr_auc(y, p)
            res["roc_auc"] = metrics.roc_auc(y, p)
            rows.append({"start": str(split.test_start), "end": str(split.test_end),
                         **res})
        results[name] = {"folds": rows,
                         "mean": {metric: float(np.mean([row[metric] for row in rows]))
                                  for metric in ("pr_auc", "roc_auc",
                                                 "daily_precision_at_k",
                                                 "daily_recall_at_k")}}
        print(name, json.dumps(results[name]["mean"]), flush=True)
    path = PATHS.reports / "fire_rule_baselines.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
