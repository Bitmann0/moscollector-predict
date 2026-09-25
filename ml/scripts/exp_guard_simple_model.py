"""Check whether a deployable simple-feature LightGBM beats the review rule.

This uses v2 event-time recorded-alarm labels and today's candidate filter.
Unknown next-day outcomes stay in the queue and denominator. They are treated
as no *recorded* signal when fitting this explicitly recorded-signal target.
"""
import datetime as dt
import json

import numpy as np
import polars as pl

from mkl import guard_queue, metrics, serve, store, train
from mkl.config import PATHS

START, END = dt.date(2023, 1, 1), dt.date(2025, 12, 31)
TEST_STARTS = (dt.date(2025, 4, 5), dt.date(2025, 7, 4),
               dt.date(2025, 10, 2))


def evaluate(frame: pl.DataFrame, score: np.ndarray, budget: int) -> dict:
    chosen = metrics._daily_top_mask(score, frame["day"].to_numpy(), budget)
    y = frame["y"].to_numpy().astype(bool)
    known = frame["known"].to_numpy()
    alerts = int(chosen.sum())
    hits = int((chosen & y).sum())
    return {"alerts": alerts, "recorded_hits": hits,
            "unknown_outcome_alerts": int((chosen & ~known).sum()),
            "recorded_hits_per_alert": hits/alerts}


def main() -> None:
    cfg = serve.load_heads()["C"]
    base = store.read_slice("object", START, END)
    drop = cfg.get("drop_feature_prefixes") or []
    base = base.select([c for c in base.columns
                        if not any(c.startswith(prefix) for prefix in drop)])
    events = pl.read_parquet(guard_queue.EVENT_DAYS)
    data = guard_queue.current_candidates(
        guard_queue.add_alarm_history(base, events)).filter(
            pl.col("day") >= START+dt.timedelta(days=30))
    labels = pl.read_parquet(PATHS.features / "label_intrusion_eventtime_v2.parquet")
    data = data.join(labels.select("obj", "day", "y", "known"),
                     on=["obj", "day"]).sort(["day", "obj"])
    features = train.feature_columns(data)
    assert not {"y", "known", "eligible"} & set(features)
    out = {"target": "future recorded guarded alarm, not confirmed intrusion",
           "features": features, "folds": []}
    for test_start in TEST_STARTS:
        test_end = test_start + dt.timedelta(days=89)
        cal_end = test_start - dt.timedelta(days=32)
        cal_start = cal_end - dt.timedelta(days=59)
        fit_end = cal_start - dt.timedelta(days=32)
        fit = data.filter(pl.col("day") <= fit_end)
        test = data.filter(pl.col("day").is_between(test_start, test_end))
        y = fit["y"].to_numpy()
        model = train._build_model("lgbm", {**cfg["params"], "n_jobs": 4},
                                   (len(y)-y.sum())/y.sum())
        model.fit(train._matrix(fit, features), y)
        pred = model.predict_proba(train._matrix(test, features))[:, 1]
        rule = guard_queue.priority_score(test)["priority_score"].to_numpy()
        fold = {"fit_end": str(fit_end), "test_start": str(test_start),
                "test_end": str(test_end), "fit_rows": len(fit),
                "test_candidates": len(test), "recorded_positives": int(test["y"].sum())}
        for budget in (1, 4):
            fold[f"top{budget}"] = {"ml": evaluate(test, pred, budget),
                                    "rule": evaluate(test, rule, budget)}
        out["folds"].append(fold)
    out["pooled"] = {}
    for budget in (1, 4):
        out["pooled"][f"top{budget}"] = {}
        for method in ("ml", "rule"):
            vals = [f[f"top{budget}"][method] for f in out["folds"]]
            alerts = sum(v["alerts"] for v in vals)
            hits = sum(v["recorded_hits"] for v in vals)
            out["pooled"][f"top{budget}"][method] = {
                "alerts": alerts, "recorded_hits": hits,
                "unknown_outcome_alerts": sum(v["unknown_outcome_alerts"] for v in vals),
                "recorded_hits_per_alert": hits/alerts}
    dst = PATHS.reports / "guard_simple_model_experiment.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out["pooled"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
