"""Paired day-block uncertainty for the six-hour retrospective experiment."""
import datetime as dt
import json

import numpy as np
import polars as pl

from mkl.config import PATHS
from mkl.metrics import _daily_top_mask


def main() -> None:
    report = json.loads((PATHS.reports / "intrusion_six_hour_6h_coverage.json").read_text(encoding="utf-8"))
    preds = pl.read_parquet(PATHS.features / "intrusion_six_hour_6h_predictions.parquet")
    models = sorted(preds["model"].unique().to_list())
    days = [dt.date(2025, 4, 5) + dt.timedelta(days=i) for i in range(270)]
    result = {"seed": 42, "resamples": 5000,
              "note": "Paired circular day-block bootstrap on explored 2025 folds; fitted models fixed; diagnostic only",
              "views": {}}
    for view in ("all", "quiet"):
        daily = {}
        for model in models:
            f = preds.filter(pl.col("model") == model).sort(["asof", "obj"])
            if view == "quiet":
                f = f.filter(pl.col("quiet"))
            picked = _daily_top_mask(f["score"].to_numpy(), f["asof"].to_numpy(), 1)
            rows = f.with_columns(pl.Series("picked", picked)).group_by("forecast_day").agg(
                pl.col("picked").sum().alias("alerts"),
                (pl.col("picked")*pl.col("y")).sum().alias("hits"))
            daily[model] = (pl.DataFrame({"forecast_day": days}).join(
                rows, on="forecast_day", how="left").fill_null(0).sort("forecast_day")
                .select("hits", "alerts").to_numpy())
            expected = report["pooled"][model][view]
            assert daily[model].sum(axis=0).tolist() == [expected["hits"], expected["alerts"]]
        intervals = {}
        for block in (14, 30):
            rng = np.random.default_rng(42)
            samples = []
            for start in (0, 90, 180):
                starts = rng.integers(90, size=(5000, (90+block-1)//block))
                local = (starts[:, :, None] + np.arange(block)) % 90
                samples.append(start + local.reshape(5000, -1)[:, :90])
            ix = np.concatenate(samples, axis=1)
            boot = {}
            for name in models:
                sums = daily[name][ix].sum(axis=1)
                boot[name] = sums[:, 0]/sums[:, 1]
            pairs = (("lgb_joint", "rule_week"), ("lgb_recorded", "rule_week"),
                     ("lgb_joint", "lgb_extended"),
                     ("lgb_recorded", "lgb_simple"))
            intervals[str(block)] = {}
            for a, b in pairs:
                diff = boot[a]-boot[b]
                observed = (report["pooled"][a][view]["precision_lower_bound"] -
                            report["pooled"][b][view]["precision_lower_bound"])
                intervals[str(block)][f"{a}_minus_{b}"] = {
                    "difference_pp": 100*observed,
                    "paired_95_percent_interval_pp": (100*np.quantile(diff, [.025, .975])).tolist()}
        result["views"][view] = intervals
    (PATHS.reports / "intrusion_six_hour_checks.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
