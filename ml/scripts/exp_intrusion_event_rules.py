"""Smoothed association rules over recent event states for C.

This adapts the industrial alarm-sequence idea to next-day, per-object risk.
Rules are learned on each fold's training block and scored on the same eligible
armed object-days and daily alert budget as the supervised C experiments.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl

from exp_intrusion_event_fusion import event_day
from mkl import cv, db, labels, metrics, serve, store
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

START, END = dt.date(2023, 1, 1), dt.date(2025, 12, 31)


def symbols(base: pl.DataFrame, event: pl.DataFrame) -> pl.DataFrame:
    data = base.select("obj", "day", "obj_armed").join(
        event, on=["obj", "day"], how="left").sort(["obj", "day"])
    data = data.with_columns([
        pl.col("evt_intrusion_alarm").fill_null(0),
        pl.col("evt_intrusion_unalarmed").fill_null(0),
        pl.col("evt_guard_states").fill_null(0),
    ])
    data = data.with_columns([
        pl.when(pl.col("evt_intrusion_alarm") > 0).then(3)
          .when(pl.col("evt_intrusion_unalarmed") >= 10).then(2)
          .when(pl.col("evt_intrusion_unalarmed") > 0).then(1)
          .otherwise(0).alias("state"),
        (pl.col("evt_guard_states") > 0).cast(pl.Int8).alias("guard_event"),
        pl.col("obj_armed").fill_null(-1).cast(pl.Int8).alias("armed"),
    ])
    data = data.with_columns([
        pl.col("state").shift(1).over("obj").fill_null(-1).alias("state_prev1"),
        pl.col("state").shift(2).over("obj").fill_null(-1).alias("state_prev2"),
        (pl.col("day") - pl.col("day").shift(1).over("obj"))
          .dt.total_days().fill_null(999).alias("gap_prev1"),
        (pl.col("day") - pl.col("day").shift(2).over("obj"))
          .dt.total_days().fill_null(999).alias("gap_prev2"),
    ])
    return data.with_columns([
        pl.when(pl.col("gap_prev1") <= 2).then(pl.col("state_prev1"))
          .otherwise(-1).alias("state_prev1"),
        pl.when(pl.col("gap_prev2") <= 4).then(pl.col("state_prev2"))
          .otherwise(-1).alias("state_prev2"),
    ]).select("obj", "day", "state", "state_prev1", "state_prev2",
              "guard_event", "armed")


def predict_rules(training: pl.DataFrame, test: pl.DataFrame) -> np.ndarray:
    prior = float(training["y"].mean())
    coarse_keys = ["state", "state_prev1", "armed"]
    fine_keys = coarse_keys + ["state_prev2", "guard_event"]
    coarse = training.group_by(coarse_keys).agg(
        pl.len().alias("n_coarse"), pl.col("y").sum().alias("pos_coarse"))
    coarse = coarse.with_columns(
        ((pl.col("pos_coarse") + 100 * prior) /
         (pl.col("n_coarse") + 100)).alias("p_coarse"))
    fine = training.group_by(fine_keys).agg(
        pl.len().alias("n_fine"), pl.col("y").sum().alias("pos_fine"))
    scored = test.join(coarse, on=coarse_keys, how="left").join(
        fine, on=fine_keys, how="left")
    p_coarse = scored["p_coarse"].fill_null(prior).to_numpy()
    n_fine = scored["n_fine"].fill_null(0).to_numpy()
    pos_fine = scored["pos_fine"].fill_null(0).to_numpy()
    return (pos_fine + 50 * p_coarse) / (n_fine + 50)


def main() -> None:
    cfg = serve.load_heads()["C"]
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_intrusion(con, armed_only=True)
    lab = con.execute("SELECT * FROM label_intrusion WHERE day BETWEEN ? AND ?",
                      [START, END]).pl()
    con.close()
    base = store.read_slice("object", START, END)
    event = pl.concat([event_day(y) for y in (2023, 2024, 2025)])
    data = symbols(base, event).join(lab, on=["obj", "day"]).sort(["day", "obj"])
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3, 90,
                             cfg["embargo_days"])
    results = {"note": "2025 folds previously used to choose C history features",
               "method": "state sequence, empirical rates with fixed 100/50 prior smoothing",
               "folds": []}
    for split in splits:
        tr = data.filter((pl.col("day") >= split.train_start) &
                         (pl.col("day") <= split.train_end))
        te = data.filter((pl.col("day") >= split.test_start) &
                         (pl.col("day") <= split.test_end))
        p = predict_rules(tr, te)
        y, days = te["y"].to_numpy(), te["day"].to_numpy()
        daily = metrics.daily_budget_summary(y, p, days, cfg["budget_per_day"])
        row = {"start": str(split.test_start), "end": str(split.test_end),
               "n": len(y), "positives": int(y.sum()),
               "pr_auc": metrics.pr_auc(y, p),
               "daily_precision": daily["daily_precision_at_k"],
               "daily_recall": daily["daily_recall_at_k"]}
        results["folds"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    results["mean"] = {key: float(np.mean([r[key] for r in results["folds"]]))
                       for key in ("pr_auc", "daily_precision", "daily_recall")}
    (PATHS.reports / "intrusion_event_rules_experiment.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results["mean"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
