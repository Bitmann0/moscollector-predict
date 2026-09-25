"""Check whether D's alerts remain useful after collapsing repeated episodes."""
from __future__ import annotations

import datetime as dt
import json
import sys

import polars as pl

from mkl import cv, metrics, serve, store, train
from mkl.config import HOLDOUT_START
from verify_head import load

sys.stdout.reconfigure(encoding="utf-8")

TEST_DAYS = 90
COOLDOWNS = (0, 3, 7)


def main() -> None:
    cfg = serve.load_heads()["D"]
    start = dt.date.fromisoformat(serve.load_heads()["D"].get(
        "window_start", "2023-01-01"))
    feats, lab = load("D", cfg, start)
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3,
                             TEST_DAYS, cfg["embargo_days"])
    rows = []
    for split in splits:
        fit = train.run("D", feats, lab, [split],
                        params=train.params_for(cfg, train.default_backend()),
                        budget_per_day=cfg["budget_per_day"],
                        horizon_days=cfg["horizon_days"],
                        budget_per_object=bool(cfg.get("budget_per_object")))
        keys = [k for k in ("ch", "obj", "day")
                if k in feats.columns and k in lab.columns]
        block = (feats.join(lab, on=keys, how="inner")
                 .filter((pl.col("day") >= split.test_start) &
                         (pl.col("day") <= split.test_end))
                 .sort(["day", "ch"]))
        risk = fit["model"].predict_proba(
            train._matrix(block, fit["feature_names"]))[:, 1]
        base = block.select(["ch", "obj", "day", "y"]).with_columns(
            pl.Series("risk", risk))
        row = {"test_start": str(split.test_start),
               "test_end": str(split.test_end), "policies": {}}
        for cooldown in COOLDOWNS:
            served = serve.alerts_over_time(
                base, cfg["budget_per_day"], entity="ch",
                cooldown_days=cooldown,
                per_object=bool(cfg.get("budget_per_object")))
            m = metrics.episodes_per_100_alerts(
                served["ch"].to_numpy(), served["day"].to_numpy(),
                served["y"].to_numpy(), served["alert"].to_numpy(),
                horizon_days=cfg["horizon_days"])
            row["policies"][str(cooldown)] = m
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    total = {}
    for cooldown in COOLDOWNS:
        vals = [r["policies"][str(cooldown)] for r in rows]
        alerts = sum(v["alerts"] for v in vals)
        caught = sum(v["episodes_caught"] for v in vals)
        episodes = sum(v["episodes"] for v in vals)
        total[str(cooldown)] = {
            "alerts": alerts, "episodes": episodes,
            "episodes_caught": caught,
            "episodes_per_100_alerts": 100 * caught / alerts if alerts else None,
            "episode_recall": caught / episodes if episodes else None,
        }
    result = {"head": "D", "budget_per_day": cfg["budget_per_day"],
              "cooldowns": COOLDOWNS, "folds": rows, "pooled": total,
              "note": "walk-forward folds; recorded wear proxy, not repair truth"}
    path = store.PATHS.reports / "d_episode_policy.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(json.dumps(total, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
