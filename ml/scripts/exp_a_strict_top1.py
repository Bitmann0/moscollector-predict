"""Evaluate A_strict as a one-channel-per-day diagnostic queue."""
from __future__ import annotations

import datetime as dt
import json
import sys

import polars as pl

from mkl import cv, metrics, serve, store, train
from verify_head import load

sys.stdout.reconfigure(encoding="utf-8")

BUDGET = 1
TEST_DAYS = 90


def evaluate_policy(block: pl.DataFrame, score: str, objects: bool) -> dict:
    y = block["y"].to_numpy()
    p = block[score].to_numpy()
    days = block["day"].to_numpy()
    obj = block["obj"].to_numpy() if objects else None
    summary = metrics.daily_budget_summary(y, p, days, BUDGET, objects=obj)
    chosen = metrics._daily_top_mask(p, days, BUDGET, objects=obj)
    episodes = metrics.episodes_per_100_alerts(
        block["ch"].to_numpy(), days, y, chosen, horizon_days=1)
    return {**summary, **episodes,
            "precision_at_k": metrics.precision_at_k(y, p, BUDGET * len(set(days))),
            "recall_at_k": metrics.recall_at_k(y, p, BUDGET * len(set(days)))}


def main() -> None:
    cfg = serve.load_heads()["A_strict"]
    start = dt.date(2023, 1, 1)
    feats, lab = load("A_strict", cfg, start)
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3,
                             TEST_DAYS, cfg["embargo_days"])
    folds = []
    for split in splits:
        fit = train.run("A_strict", feats, lab, [split],
                        params=train.params_for(cfg, train.default_backend()),
                        budget_per_day=BUDGET)
        keys = [k for k in ("ch", "obj", "day")
                if k in feats.columns and k in lab.columns]
        block = (feats.join(lab, on=keys, how="inner")
                 .filter((pl.col("day") >= split.test_start) &
                         (pl.col("day") <= split.test_end) &
                         pl.col("obj").is_not_null())
                 .sort(["day", "ch"]))
        risk = fit["model"].predict_proba(
            train._matrix(block, fit["feature_names"]))[:, 1]
        block = block.with_columns(pl.Series("model", risk))
        block = block.with_columns(pl.col("n_bad_w7").fill_null(0).cast(pl.Float64)
                                   .alias("rule"))
        row = {"test_start": str(split.test_start),
               "test_end": str(split.test_end),
               "model": evaluate_policy(block, "model", True),
               "rule": evaluate_policy(block, "rule", True)}
        folds.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    pooled = {}
    for name in ("model", "rule"):
        vals = [x[name] for x in folds]
        pooled[name] = {
            "alerts": sum(x["daily_alerts"] for x in vals),
            "hits": sum(x["episodes_caught"] for x in vals),
            "episodes": sum(x["episodes"] for x in vals),
            "daily_precision": sum(x["daily_precision_at_k"] * x["daily_alerts"]
                                    for x in vals) / sum(x["daily_alerts"] for x in vals),
            "episodes_per_100_alerts": 100 * sum(x["episodes_caught"] for x in vals)
                / sum(x["daily_alerts"] for x in vals),
            "episode_recall": sum(x["episodes_caught"] for x in vals)
                / sum(x["episodes"] for x in vals),
        }
    result = {"head": "A_strict", "budget_per_day": BUDGET,
              "target": "recorded_sensor_anomaly_next_day",
              "folds": folds, "pooled": pooled,
              "note": "walk-forward; proxy label, one channel per day"}
    path = store.PATHS.reports / "a_strict_top1.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(pooled, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
