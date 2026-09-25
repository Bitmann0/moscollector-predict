"""Temporal audit of D's actual threshold + daily budget + cooldown policy.

Each test block has a preceding 30-day threshold window, a separate 30-day
calibration window, and an embargo before fitting. The last seven days of the
threshold window are left unused: their future-seven-day labels would overlap
the test block and would not have been known at the decision time.
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
from mkl.config import EQUIPMENT_STYPES

sys.stdout.reconfigure(encoding="utf-8")

TEST_DAYS = 90
N_SPLITS = 5
THRESHOLD_DAYS = 30
CALIBRATION_DAYS = 30
MIN_PRECISION = 0.70
MIN_ALERTS = 30


def _period(block: pl.DataFrame, start: dt.date, end: dt.date) -> pl.DataFrame:
    return block.filter((pl.col("day") >= start) & (pl.col("day") <= end))


def _scored(block: pl.DataFrame, model, iso, names: list[str],
            feature: str | None = None) -> pl.DataFrame:
    if feature is None:
        raw = model.predict_proba(train._matrix(block, names))[:, 1]
        risk = calibrate.apply(iso, raw)
    else:
        risk = block[feature].fill_null(0).to_numpy().astype(float)
    return block.select("ch", "obj", "day", "y").with_columns(
        pl.Series("risk", risk))


def _summary(scored: pl.DataFrame, threshold: float | None,
             cfg: dict) -> dict:
    served = serve.alerts_over_time(
        scored, cfg["budget_per_day"], entity="ch", cooldown_days=7,
        per_object=bool(cfg.get("budget_per_object")), threshold=threshold)
    picked = served.filter(pl.col("alert"))
    raw = served.filter(pl.col("y") == 1)
    episodes = metrics.episodes_per_100_alerts(
        served["ch"].to_numpy(), served["day"].to_numpy(),
        served["y"].to_numpy(), served["alert"].to_numpy(),
        horizon_days=cfg["horizon_days"])
    episodes = {k: (v if not isinstance(v, float) or math.isfinite(v) else None)
                for k, v in episodes.items()}
    return {"alerts": picked.height, "true_alerts": int(picked["y"].sum()),
            "precision": float(picked["y"].mean()) if picked.height else None,
            "recall": float(picked["y"].sum() / raw.height) if raw.height else None,
            **episodes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", type=dt.date.fromisoformat,
                        help="last observable label day; default is latest available")
    args = parser.parse_args()
    cfg = serve.load_heads()["D"]
    start = dt.date(2023, 1, 1)
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    labels.build_for_head(con, cfg)
    lab = con.execute(
        "SELECT * FROM label_wear WHERE day >= ? AND day <= ?",
        [start, args.end_date or dt.date.max]).pl()
    con.close()
    feats = store.read_slice(cfg["feature_set"], start, lab["day"].max())
    drop = cfg.get("drop_feature_prefixes") or []
    if drop:
        feats = feats.select([c for c in feats.columns
                              if not any(c.startswith(prefix) for prefix in drop)])
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()),
                             N_SPLITS, TEST_DAYS, cfg["embargo_days"])
    keys = [k for k in ("ch", "obj", "day")
            if k in feats.columns and k in lab.columns]
    data = feats.join(lab, on=keys, how="inner").sort(["day", "obj", "ch"])
    baseline_feature = "n_bad_w7" if "n_bad_w7" in data.columns else None
    rows = []
    for split in splits:
        # The test starts only after every threshold-window label has matured.
        threshold_end = split.test_start - dt.timedelta(days=cfg["horizon_days"] + 1)
        threshold_start = threshold_end - dt.timedelta(days=THRESHOLD_DAYS - 1)
        calibration_end = threshold_start - dt.timedelta(days=1)
        calibration_start = calibration_end - dt.timedelta(days=CALIBRATION_DAYS - 1)
        training_end = calibration_start - dt.timedelta(days=cfg["embargo_days"] + 1)
        fit = train.run(
            "D", feats, lab,
            [cv.Split(start, training_end, calibration_start, calibration_end)],
            params=train.params_for(cfg, "lgbm"), backend="lgbm",
            budget_per_day=cfg["budget_per_day"],
            horizon_days=cfg["horizon_days"],
            budget_per_object=bool(cfg.get("budget_per_object")))
        model, names = fit["model"], fit["feature_names"]
        if model is None:
            raise ValueError("D fit had no train/calibration positives")
        cal = _period(data, calibration_start, calibration_end)
        thr = _period(data, threshold_start, threshold_end)
        test = _period(data, split.test_start, split.test_end)
        iso = calibrate.fit_isotonic(
            model.predict_proba(train._matrix(cal, names))[:, 1],
            cal["y"].to_numpy())
        threshold_scores = _scored(thr, model, iso, names)
        pick = metrics.daily_target_operating_point(
            threshold_scores["y"].to_numpy(),
            threshold_scores["risk"].to_numpy(),
            threshold_scores["day"].to_numpy(), cfg["budget_per_day"],
            min_precision=MIN_PRECISION,
            objects=threshold_scores["obj"].to_numpy(),
            min_alerts=MIN_ALERTS)
        selected_threshold = (float(pick["threshold"]) if pick.get("feasible")
                              else float(np.nextafter(1.0, np.inf)))
        scored = _scored(test, model, iso, names)
        model_result = _summary(scored, selected_threshold, cfg)
        baseline_result = None
        baseline_thresholded = None
        baseline_pick = None
        if baseline_feature:
            baseline_result = _summary(
                _scored(test, model, iso, names, baseline_feature), None, cfg)
            baseline_threshold_scores = _scored(
                thr, model, iso, names, baseline_feature)
            baseline_pick = metrics.daily_target_operating_point(
                baseline_threshold_scores["y"].to_numpy(),
                baseline_threshold_scores["risk"].to_numpy(),
                baseline_threshold_scores["day"].to_numpy(),
                cfg["budget_per_day"], min_precision=MIN_PRECISION,
                objects=baseline_threshold_scores["obj"].to_numpy(),
                min_alerts=MIN_ALERTS)
            baseline_threshold = (float(baseline_pick["threshold"])
                                  if baseline_pick.get("feasible") else
                                  float(np.nextafter(
                                      baseline_threshold_scores["risk"].max(),
                                      np.inf)))
            baseline_thresholded = _summary(
                _scored(test, model, iso, names, baseline_feature),
                baseline_threshold, cfg)
        # All test rows lacking a known outcome must remain unknown, never 0.
        feat_test = _period(feats, split.test_start, split.test_end).filter(
            pl.col("stype").is_in(EQUIPMENT_STYPES))
        unknown = feat_test.height - test.height
        row = {
            "test_start": str(split.test_start), "test_end": str(split.test_end),
            "training_end": str(training_end),
            "calibration": [str(calibration_start), str(calibration_end)],
            "threshold_window": [str(threshold_start), str(threshold_end)],
            "threshold": selected_threshold if math.isfinite(selected_threshold) else None,
            "threshold_feasible": bool(pick.get("feasible")),
            "threshold_selection": pick, "known_rows": test.height,
            "unknown_rows": unknown, "model": model_result,
            "baseline_feature": baseline_feature, "baseline": baseline_result,
            "baseline_threshold_selection": baseline_pick,
            "baseline_thresholded": baseline_thresholded,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False, allow_nan=False), flush=True)
    result = {"head": "D", "evaluated_through": str(lab["day"].max()),
              "policy": "threshold; top_3_per_day; cooldown_7d_no_backfill",
              "baseline": baseline_feature, "folds": rows,
              "caution": "Recorded equipment-state proxy, not confirmed physical wear."
              " Previously examined years are not a fresh independent holdout."}
    path = store.PATHS.reports / "d_live_policy_temporal.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                               allow_nan=False), encoding="utf-8")
    print(f"saved {path}", flush=True)


if __name__ == "__main__":
    main()
