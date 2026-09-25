"""Retrospective next-day C queue using only information available at issue time.

Outcomes with insufficient future telemetry are unknown, not known negatives.
For operations, every current candidate is ranked regardless of future outcome.
The calendar was already explored; results are not a blind holdout.
"""
import datetime as dt
import json
import sys
import time

import numpy as np
import polars as pl

from exp_intrusion_precise_suite import build_data
from mkl import cv, metrics, train
from mkl.config import PATHS

VARIANTS = ("rule_week", "conditional_simple", "conditional_full",
            "recorded_full", "joint_full")
STARTS = (dt.date(2025, 4, 5), dt.date(2025, 7, 4), dt.date(2025, 10, 2))


def evaluate(frame: pl.DataFrame, score: np.ndarray, *, quiet: bool, budget: int) -> dict:
    if quiet:
        mask = frame["quiet"].to_numpy()
        frame, score = frame.filter(pl.Series(mask)), score[mask]
    picked = metrics._daily_top_mask(score, frame["day"].to_numpy(), budget)
    y = frame["y"].to_numpy().astype(bool)
    known = frame["known"].to_numpy()
    alerts = int(picked.sum())
    hits = int((picked & y).sum())
    unknown = int((picked & ~known).sum())
    positives = int(y.sum())
    return {"candidates": len(frame), "known_candidates": int(known.sum()),
            "positive_recorded": positives, "alerts": alerts, "recorded_hits": hits,
            "unknown_outcome_alerts": unknown,
            "recorded_hits_per_alert": hits/alerts if alerts else None,
            "recorded_recall": hits/positives if positives else None}


def model_scores(fit, cal, test, cols, cfg, target):
    y = fit[target].to_numpy()
    model = train._build_model("lgbm", {**cfg["params"], "n_jobs": 4},
                               (len(y)-y.sum())/y.sum())
    model.fit(train._matrix(fit, cols), y)
    return (model.predict_proba(train._matrix(cal, cols))[:, 1],
            model.predict_proba(train._matrix(test, cols))[:, 1])


def main() -> None:
    data, base_cols, full_cols, cfg = build_data(operational=True)
    assert "eligible" not in full_cols and "known" not in full_cols
    count_cols = ["exact_count_1", "exact_count_7", "exact_count_30", "exact_age"]
    simple_cols = base_cols + count_cols
    report = {"target": "recorded alarm tomorrow with guard armed at alarm time",
              "candidate": "current object-store row, armed EOD, guard message <=7d old",
              "outcome": "positive recorded; negative only if tomorrow object telemetry and no unresolved armed state; otherwise unknown",
              "note": "2025 periods already explored; no blind validation; model scores are not calibrated probabilities",
              "folds": [], "overall_candidates": len(data),
              "overall_known": int(data["known"].sum()),
              "overall_recorded_positive": int(data["y"].sum())}
    pred_rows = []
    for test_start in STARTS:
        test_end = test_start + dt.timedelta(days=89)
        cal_end = test_start - dt.timedelta(days=32)
        cal_start = cal_end - dt.timedelta(days=59)
        fit_end = cal_start - dt.timedelta(days=32)
        past = data.filter(pl.col("day") <= fit_end)
        fit = past.filter(pl.col("known"))
        cal = data.filter(pl.col("day").is_between(cal_start, cal_end))
        test = data.filter(pl.col("day").is_between(test_start, test_end))
        fold = {"fit_end": str(fit_end), "cal_start": str(cal_start),
                "cal_end": str(cal_end), "test_start": str(test_start),
                "test_end": str(test_end), "fit_known": len(fit),
                "fit_positive": int(fit["y"].sum()), "models": {}}
        full_conditional = None
        for name in VARIANTS:
            start = time.perf_counter()
            if name == "rule_week":
                sc, st = (f["exact_count_7"].to_numpy().astype(float)
                          for f in (cal, test))
            elif name == "joint_full":
                assert full_conditional is not None
                kc, kt = model_scores(past, cal, test, full_cols, cfg, "known")
                sc, st = full_conditional[0]*kc, full_conditional[1]*kt
            else:
                tr = past if name == "recorded_full" else fit
                cols = simple_cols if name == "conditional_simple" else full_cols
                sc, st = model_scores(tr, cal, test, cols, cfg, "y")
                if name == "conditional_full":
                    full_conditional = (sc.copy(), st.copy())
            fold["models"][name] = {"seconds": time.perf_counter()-start}
            for subset in ("all", "quiet"):
                for k in (1, 4):
                    key = f"{subset}_top{k}"
                    fold["models"][name][key] = evaluate(test, st, quiet=subset == "quiet", budget=k)
                    fold["models"][name][f"selection_{key}"] = evaluate(
                        cal, sc, quiet=subset == "quiet", budget=k)
            pred_rows.append(test.select("obj", "day", "y", "known", "quiet")
                             .with_columns(pl.Series("score", st, dtype=pl.Float64),
                                           pl.lit(name).alias("model")))
            print(test_start, name, fold["models"][name]["all_top4"], flush=True)
        report["folds"].append(fold)
        (PATHS.tmp / "intrusion_operational_queue_checkpoint.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pl.concat(pred_rows).write_parquet(
        PATHS.features / "intrusion_operational_queue_predictions.parquet")
    report["pooled"] = {}
    for name in VARIANTS:
        report["pooled"][name] = {}
        for subset in ("all", "quiet"):
            for k in (1, 4):
                key = f"{subset}_top{k}"
                rows = [f["models"][name][key] for f in report["folds"]]
                pooled = {field: sum(r[field] for r in rows)
                          for field in ("candidates", "known_candidates",
                                        "positive_recorded", "alerts", "recorded_hits",
                                        "unknown_outcome_alerts")}
                pooled["recorded_hits_per_alert"] = (
                    pooled["recorded_hits"]/pooled["alerts"] if pooled["alerts"] else None)
                pooled["recorded_recall"] = (
                    pooled["recorded_hits"]/pooled["positive_recorded"]
                    if pooled["positive_recorded"] else None)
                report["pooled"][name][key] = pooled
    (PATHS.reports / "intrusion_operational_queue.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print("POOLED", json.dumps(report["pooled"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
