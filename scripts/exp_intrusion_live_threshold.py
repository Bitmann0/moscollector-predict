"""Check whether C history survives the rolling service alert policy.

Threshold metrics are measured on the same 30-day block used to choose the
threshold; this is an operational feasibility check, not future performance.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl

from exp_intrusion_sequence import with_history
from mkl import calibrate, config, db, labels, metrics, serve, store, train
from mkl.config import PATHS
from mkl.cv import Split, live_windows

sys.stdout.reconfigure(encoding="utf-8")


def evaluate(name: str, feats: pl.DataFrame, lab: pl.DataFrame,
             dates: dict, cfg: dict, start: dt.date) -> dict:
    split = Split(start, dates["training_end"],
                  dates["calibration_start"], dates["threshold_end"])
    fit = train.run("C", feats, lab, [split],
                    params=train.params_for(cfg, "lgbm"),
                    budget_per_day=cfg["budget_per_day"], backend="lgbm")
    valid = feats.join(lab, on=["obj", "day"]).sort(["day", "obj"])
    cal = valid.filter((pl.col("day") >= dates["calibration_start"]) &
                       (pl.col("day") <= dates["calibration_end"]))
    threshold = valid.filter((pl.col("day") >= dates["threshold_start"]) &
                             (pl.col("day") <= dates["threshold_end"]))
    names = fit["feature_names"]
    model = fit["model"]
    iso = calibrate.fit_isotonic(
        model.predict_proba(train._matrix(cal, names))[:, 1],
        cal["y"].to_numpy())
    p = calibrate.apply(
        iso, model.predict_proba(train._matrix(threshold, names))[:, 1])
    y, days = threshold["y"].to_numpy(), threshold["day"].to_numpy()
    pick = metrics.daily_target_operating_point(
        y, p, days, cfg["budget_per_day"], min_precision=0.7,
        min_alerts=30)
    chosen = (pick["threshold"] if pick["feasible"]
              else float(np.nextafter(1.0, np.inf)))
    actual = metrics.daily_budget_summary(
        y, p, days, cfg["budget_per_day"], threshold=chosen)
    return {"name": name, "validation_pr_auc": fit["mean"]["pr_auc"],
            "validation_daily_precision": fit["mean"]["daily_precision_at_k"],
            "threshold_feasible": pick["feasible"], "threshold": chosen,
            "threshold_precision": actual["daily_precision_at_k"]
            if actual["daily_alerts"] else None,
            "threshold_recall": actual["daily_recall_at_k"],
            "threshold_alerts": actual["daily_alerts"]}


def main() -> None:
    cfg = serve.load_heads()["C"]
    start = dt.date.fromisoformat(
        config.head_a_choice().get("window_start", "2023-01-01"))
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_intrusion(con, armed_only=True)
    lab = con.execute("SELECT * FROM label_intrusion WHERE day >= ?", [start]).pl()
    con.close()
    dates = live_windows(lab["day"].max(), cfg["embargo_days"])
    base = store.read_slice("object", start, dates["threshold_end"])
    extended, names = with_history(base)
    alarm_only = extended.drop([c for c in names if not c.startswith(
        ("seq_n_alarms_", "seq_n_intrusion_"))])
    results = {"note": "threshold-block metrics are selected on this same block",
               "dates": {k: str(v) for k, v in dates.items()},
               "variants": []}
    for name, frame in (("baseline", base), ("history_tree", extended),
                        ("alarm_history_tree", alarm_only)):
        row = evaluate(name, frame, lab, dates, cfg, start)
        results["variants"].append(row)
        print(json.dumps(row, ensure_ascii=False, allow_nan=False), flush=True)
    out = PATHS.reports / "intrusion_sequence_live_threshold.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2,
                              allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
