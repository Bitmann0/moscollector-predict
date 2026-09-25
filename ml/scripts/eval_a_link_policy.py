"""Evaluate A_link's complete daily policy without choosing on test periods.

The earlier 0.70 operating point abstained. This script compares two
predeclared minimum-precision policies (0.70 and the team's 0.50 pilot gate),
plus unconditional top-20, over five successive future blocks. Each threshold
is chosen on an earlier, fully observed 30-day window. No test labels determine
whether a block emits alerts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys

import numpy as np
import polars as pl

from mkl import calibrate, cv, db, labels, metrics, serve, store, train
try:
    from scripts.eval_d_live_policy import _period, _scored
except ModuleNotFoundError:  # direct: python scripts/eval_a_link_policy.py
    from eval_d_live_policy import _period, _scored

sys.stdout.reconfigure(encoding="utf-8")

N_SPLITS = 5
TEST_DAYS = 90
WINDOW_DAYS = 30
MIN_ALERTS = 30
COOLDOWN_DAYS = 7
BASELINE_FEATURE = "gap_vs_own_rhythm"


def _select(scores: pl.DataFrame, cfg: dict, min_precision: float) -> dict:
    return metrics.daily_target_operating_point(
        scores["y"].fill_null(0).to_numpy(), scores["risk"].to_numpy(),
        scores["day"].to_numpy(), cfg["budget_per_day"],
        min_precision=min_precision, min_alerts=MIN_ALERTS)


def _threshold(pick: dict, scores: pl.DataFrame) -> float:
    return (float(pick["threshold"]) if pick.get("feasible")
            else float(np.nextafter(scores["risk"].max(), np.inf)))


def _summary_unknown(scored: pl.DataFrame, threshold: float | None,
                     cfg: dict) -> dict:
    """Rank every live candidate; unknown outcomes consume slots, not negatives."""
    served = serve.alerts_over_time(
        scored, cfg["budget_per_day"], entity="ch",
        cooldown_days=COOLDOWN_DAYS, threshold=threshold)
    chosen = served.filter(pl.col("alert"))
    hits = chosen.filter(pl.col("y") == 1).height
    unknown = chosen["y"].null_count()
    positive = served.filter(pl.col("y") == 1).height
    episodes = metrics.episodes_per_100_alerts(
        served["ch"].to_numpy(), served["day"].to_numpy(),
        served["y"].fill_null(0).to_numpy(), served["alert"].to_numpy(),
        horizon_days=cfg["horizon_days"])
    episodes = {k: (v if not isinstance(v, float) or math.isfinite(v) else None)
                for k, v in episodes.items()}
    return {"alerts": chosen.height, "hits": hits,
            "known_misses": chosen.height - hits - unknown,
            "unknown_alerts": unknown,
            "precision_lower_bound": hits / chosen.height if chosen.height else None,
            "precision_known_only": hits / (chosen.height - unknown)
            if chosen.height > unknown else None,
            "recall_known": hits / positive if positive else None,
            **episodes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", type=dt.date.fromisoformat,
                        help="last mature label day; default latest available")
    args = parser.parse_args()
    cfg = serve.load_heads()["A_link"]
    start = dt.date(2023, 1, 1)
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    labels.build_for_head(con, cfg)
    lab = con.execute(
        "SELECT * FROM label_link WHERE day >= ? AND day <= ?",
        [start, args.end_date or dt.date.max]).pl()
    con.close()
    feats = store.read_slice(cfg["feature_set"], start, lab["day"].max())
    drop = cfg.get("drop_feature_prefixes") or []
    if drop:
        feats = feats.select([c for c in feats.columns
                              if not any(c.startswith(prefix) for prefix in drop)])
    keys = [k for k in ("ch", "obj", "day")
            if k in feats.columns and k in lab.columns]
    # An inner join would remove channels with unresolved outcomes BEFORE
    # ranking. In live service they still take budget slots, so keep them here.
    data = feats.join(lab, on=keys, how="left").sort(["day", "obj", "ch"])
    if BASELINE_FEATURE not in data.columns:
        raise ValueError(f"missing baseline feature {BASELINE_FEATURE}")
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()),
                             N_SPLITS, TEST_DAYS, cfg["embargo_days"])
    rows = []
    for split in splits:
        threshold_end = split.test_start - dt.timedelta(days=cfg["horizon_days"] + 1)
        threshold_start = threshold_end - dt.timedelta(days=WINDOW_DAYS - 1)
        calibration_end = threshold_start - dt.timedelta(days=1)
        calibration_start = calibration_end - dt.timedelta(days=WINDOW_DAYS - 1)
        training_end = calibration_start - dt.timedelta(days=cfg["embargo_days"] + 1)
        fit = train.run(
            "A_link", feats, lab,
            [cv.Split(start, training_end, calibration_start, calibration_end)],
            params=train.params_for(cfg, "lgbm"), backend="lgbm",
            budget_per_day=cfg["budget_per_day"],
            horizon_days=cfg["horizon_days"])
        model, names = fit["model"], fit["feature_names"]
        if model is None:
            raise ValueError("A_link has no train/calibration positives")
        cal = _period(data, calibration_start, calibration_end).filter(
            pl.col("y").is_not_null())
        thr = _period(data, threshold_start, threshold_end)
        test = _period(data, split.test_start, split.test_end)
        iso = calibrate.fit_isotonic(
            model.predict_proba(train._matrix(cal, names))[:, 1],
            cal["y"].to_numpy())
        threshold_scores = _scored(thr, model, iso, names)
        test_scores = _scored(test, model, iso, names)
        baseline_threshold_scores = _scored(
            thr, model, iso, names, BASELINE_FEATURE)
        baseline_test_scores = _scored(
            test, model, iso, names, BASELINE_FEATURE)
        policies = {}
        for minimum in (0.70, 0.50):
            key = f"min_precision_{minimum:.2f}"
            model_pick = _select(threshold_scores, cfg, minimum)
            baseline_pick = _select(baseline_threshold_scores, cfg, minimum)
            policies[key] = {
                "selection": model_pick,
                "model": _summary_unknown(test_scores,
                                          _threshold(model_pick, threshold_scores),
                                          cfg),
                "baseline_selection": baseline_pick,
                "baseline": _summary_unknown(
                    baseline_test_scores,
                    _threshold(baseline_pick, baseline_threshold_scores),
                    cfg),
            }
        policies["fixed_top_20"] = {
            "model": _summary_unknown(test_scores, None, cfg),
            "baseline": _summary_unknown(baseline_test_scores, None, cfg)}
        unknown = test["y"].null_count()
        row = {"test_start": str(split.test_start), "test_end": str(split.test_end),
               "training_end": str(training_end),
               "calibration": [str(calibration_start), str(calibration_end)],
               "threshold_window": [str(threshold_start), str(threshold_end)],
               "known_rows": test.height - unknown, "unknown_rows": unknown,
               "policies": policies}
        rows.append(row)
        compact = {key: {"feasible": value.get("selection", {}).get("feasible"),
                         "model": {k: value["model"][k]
                                   for k in ("alerts", "hits", "unknown_alerts",
                                             "precision_lower_bound", "recall_known",
                                             "episodes_per_100_alerts")},
                         "baseline": {k: value["baseline"][k]
                                      for k in ("alerts", "precision_lower_bound")}}
                   for key, value in policies.items()}
        print(json.dumps({"test_start": row["test_start"],
                          "test_end": row["test_end"],
                          "policies": compact}, ensure_ascii=False,
                         allow_nan=False), flush=True)
    result = {"head": "A_link", "label_variant": cfg["variant"],
              "evaluated_through": str(lab["day"].max()),
              "policy": "daily_top_20; calibrated_prior_threshold; cooldown_7d_no_backfill",
              "baseline": BASELINE_FEATURE, "folds": rows,
              "caution": "L9c is a telemetry proxy. Its gap event requires a "
                         "later return observation; final channel reports are "
                         "unknown, including permanent disappearance and planned "
                         "decommissioning. Previously explored years are not blind "
                         "holdouts."}
    path = store.PATHS.reports / "a_link_live_policy_temporal.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                               allow_nan=False), encoding="utf-8")
    print(f"saved {path}", flush=True)


if __name__ == "__main__":
    main()
