"""Paired comparisons and history-only rules on the deployable daily C queue.

Diagnostic on already explored 2025 periods; cannot establish a blind winner.
"""
import datetime as dt
import json

import numpy as np
import polars as pl

from exp_intrusion_precise_suite import build_data
from mkl import metrics
from mkl.config import PATHS


def evaluate(frame, budget):
    chosen = metrics._daily_top_mask(frame["score"].to_numpy(),
                                     frame["day"].to_numpy(), budget)
    y = frame["y"].to_numpy().astype(bool)
    known = frame["known"].to_numpy()
    return {"hits": int((chosen & y).sum()), "alerts": int(chosen.sum()),
            "unknown": int((chosen & ~known).sum())}


def main():
    report = json.loads((PATHS.reports / "intrusion_operational_queue.json").read_text(encoding="utf-8"))
    scores = pl.read_parquet(PATHS.features / "intrusion_operational_queue_predictions.parquet")
    start, end = dt.date(2025, 4, 5), dt.date(2025, 12, 30)
    data, _, _, _ = build_data(operational=True)
    d = data.filter(pl.col("day").is_between(start, end)).select(
        "obj", "day", "y", "known", "quiet", "exact_count_7", "exact_count_30", "exact_age")
    # Lexicographic priority: last seven days, then last 30, then recency.
    # Coefficients guarantee tie breakers never overturn a seven-day count.
    rule = d.with_columns((pl.col("exact_count_7")*32 + pl.col("exact_count_30")/32
                           + 1/(1024*(pl.col("exact_age")+1))).cast(pl.Float64)
                          .alias("score"), pl.lit("rule_history").alias("model"))
    scores = pl.concat([scores, rule.select(scores.columns)])
    days = [start + dt.timedelta(days=i) for i in range(270)]
    models = sorted(scores["model"].unique().to_list())
    out = {"note": "Post-hoc history rule; 5000 paired day-block resamples within explored test folds; fixed fitted models",
           "seed": 42, "resamples": 5000, "views": {}}
    for view in ("all", "quiet"):
        for budget in (1, 4):
            daily, summaries = {}, {}
            for model in models:
                f = scores.filter(pl.col("model") == model).sort(["day", "obj"])
                if view == "quiet":
                    f = f.filter(pl.col("quiet"))
                s = evaluate(f, budget)
                s["recorded_hits_per_alert"] = s["hits"]/s["alerts"]
                summaries[model] = s
                if model in report["pooled"]:
                    expected = report["pooled"][model][f"{view}_top{budget}"]
                    assert (s["hits"], s["alerts"], s["unknown"]) == (
                        expected["recorded_hits"], expected["alerts"],
                        expected["unknown_outcome_alerts"])
                f = f.with_columns(pl.Series("picked", metrics._daily_top_mask(
                    f["score"].to_numpy(), f["day"].to_numpy(), budget)))
                by_day = f.group_by("day").agg(
                    (pl.col("picked")*pl.col("y")).sum().alias("hits"),
                    pl.col("picked").sum().alias("alerts"))
                daily[model] = (pl.DataFrame({"day": days}).join(
                    by_day, on="day", how="left").fill_null(0).sort("day")
                    .select("hits", "alerts").to_numpy())
            intervals = {}
            for block in (14, 30):
                rng = np.random.default_rng(42)
                chunks = []
                for i in (0, 90, 180):
                    starts = rng.integers(90, size=(5000, (90+block-1)//block))
                    ix = (starts[:, :, None]+np.arange(block)) % 90
                    chunks.append(i+ix.reshape(5000, -1)[:, :90])
                ix = np.concatenate(chunks, axis=1)
                boot = {}
                for model in models:
                    sums = daily[model][ix].sum(axis=1)
                    boot[model] = sums[:, 0]/sums[:, 1]
                pairs = (("recorded_full", "rule_week"),
                         ("recorded_full", "rule_history"),
                         ("recorded_full", "conditional_full"),
                         ("joint_full", "rule_week"))
                intervals[str(block)] = {f"{a}_minus_{b}": {
                    "difference_pp": 100*(summaries[a]["recorded_hits_per_alert"]-
                                          summaries[b]["recorded_hits_per_alert"]),
                    "paired_95_percent_interval_pp": (100*np.quantile(
                        boot[a]-boot[b], [.025, .975])).tolist()}
                    for a, b in pairs}
            out["views"][f"{view}_top{budget}"] = {"models": summaries,
                                                   "block_days": intervals}
    (PATHS.reports / "intrusion_operational_checks.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(out["views"]["all_top4"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
