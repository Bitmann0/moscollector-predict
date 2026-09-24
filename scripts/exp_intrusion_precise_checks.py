"""Fixed recurrence baselines and paired time-block uncertainty for C.

Post-hoc diagnostics on already explored folds, not a new model selection set.
Bootstrap resamples entire days across objects, in 14/30-day blocks within
each test fold; it conditions on the fitted models and observed objects.
"""
import json

import numpy as np
import polars as pl

from exp_intrusion_precise_suite import build_data
from mkl.config import PATHS
from mkl.metrics import _daily_top_mask


def main():
    suite = json.loads((PATHS.reports / "intrusion_precise_suite.json").read_text(encoding="utf-8"))
    scores = pl.read_parquet(PATHS.features / "intrusion_precise_suite_predictions.parquet")
    data, _, _, _ = build_data()
    keys = scores.filter(pl.col("model") == "exact_lgb").drop("p", "model")
    rules = keys.join(data.select("obj", "day", "exact_count_7", "exact_age"), on=["obj", "day"])
    for name, expr in (("rule_count7", pl.col("exact_count_7")),
                       ("rule_recency", -pl.col("exact_age"))):
        scores = pl.concat([scores, rules.select(
            "obj", "day", "y", "quiet", expr.cast(pl.Float64).alias("p"),
            pl.lit(name).alias("model"))])
    models = sorted(scores["model"].unique().to_list())
    days = sorted(scores["day"].unique().to_list())
    result = {"note": __doc__, "resamples": 5000, "seed": 42,
              "rule_ties": "stable object ID order; quiet count7 is constant, recency may distinguish older alarms",
              "views": {}}
    fold_indices = [np.array([i for i, d in enumerate(days)
                    if f["test_start"] <= str(d) <= f["test_end"]]) for f in suite["folds"]]
    for view in ("all", "quiet"):
        for k in (1, 4):
            arrays, summary = {}, {}
            for model in models:
                frame = scores.filter(pl.col("model") == model).sort(["day", "obj"])
                if view == "quiet":
                    frame = frame.filter(pl.col("quiet"))
                picked = _daily_top_mask(frame["p"].to_numpy(), frame["day"].to_numpy(), k)
                daily = frame.with_columns(pl.Series("picked", picked)).group_by("day").agg(
                    pl.col("picked").sum().alias("alerts"),
                    (pl.col("picked") * pl.col("y")).sum().alias("hits"))
                daily = pl.DataFrame({"day": days}).join(daily, on="day", how="left").fill_null(0).sort("day")
                arrays[model] = daily.select("hits", "alerts").to_numpy()
                h, a = arrays[model].sum(axis=0)
                summary[model] = {"hits": int(h), "alerts": int(a), "precision": float(h/a)}
                if model in suite["pooled"]:
                    expected = suite["pooled"][model][view][f"top{k}"]
                    assert int(h) == expected["hits"] and int(a) == expected["alerts"]
            checks = {}
            for block in (14, 30):
                rng = np.random.default_rng(42)
                samples = []
                for ix in fold_indices:
                    n = len(ix)
                    starts = rng.integers(n, size=(5000, (n+block-1)//block))
                    offsets = (starts[:, :, None] + np.arange(block)) % n
                    samples.append(ix[offsets.reshape(5000, -1)[:, :n]])
                indices = np.concatenate(samples, axis=1)
                boot = {}
                for model in models:
                    sums = arrays[model][indices].sum(axis=1)
                    boot[model] = sums[:, 0] / sums[:, 1]
                pairs = (("exact_history_lgb", "legacy_lgb"),
                         ("exact_history_lgb", "exact_lgb"),
                         ("exact_history_lgb", "exact_counts_lgb"),
                         ("exact_history_lgb", "rule_count7"),
                         ("exact_history_lgb", "rule_recency"))
                checks[str(block)] = {f"{a}_minus_{b}": {
                    "difference_pp": 100*(summary[a]["precision"]-summary[b]["precision"]),
                    "paired_95_percent_interval_pp": (100*np.quantile(boot[a]-boot[b], [.025, .975])).tolist()
                } for a, b in pairs}
            result["views"][f"{view}_top{k}"] = {"models": summary, "block_days": checks}
    (PATHS.reports / "intrusion_precise_checks.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result["views"]["all_top4"], ensure_ascii=False))


if __name__ == "__main__":
    main()
