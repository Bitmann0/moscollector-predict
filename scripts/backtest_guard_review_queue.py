"""Recheck the shipped manual-review rule on all 270 explored 2025 days.

The denominator includes selected rows whose next-day outcome is unknown.
This is retrospective validation against recorded SMVU alarms, not incidents.
"""
import datetime as dt
import json

import polars as pl

from mkl import guard_queue, store
from mkl.config import PATHS

START = dt.date(2025, 4, 5)
END = dt.date(2025, 12, 30)


def main() -> None:
    base = store.read_slice("object", START, END, columns=[
        "obj", "day", "obj_armed", "days_since_arm_event"])
    events = pl.read_parquet(guard_queue.EVENT_DAYS)
    labels = pl.read_parquet(PATHS.features / "label_intrusion_eventtime_v2.parquet")
    candidates = guard_queue.current_candidates(base)
    scored = guard_queue.priority_score(
        guard_queue.add_alarm_history(candidates, events)).join(
            labels.select("obj", "day", "y", "known"),
            on=["obj", "day"], how="left")
    if scored["known"].null_count():
        raise ValueError("v2 labels do not cover every historical candidate")
    folds = []
    for i in range(3):
        start = START + dt.timedelta(days=90*i)
        end = start + dt.timedelta(days=89)
        block = scored.filter(pl.col("day").is_between(start, end))
        picked = pl.concat([day.sort(["priority_score", "obj"],
                                      descending=[True, False]).head(4)
                            for day in block.partition_by("day", maintain_order=True)])
        hits = int(picked["y"].sum())
        folds.append({"start": str(start), "end": str(end),
                      "candidate_rows": block.height,
                      "recorded_positives": int(block["y"].sum()),
                      "alerts": picked.height, "recorded_hits": hits,
                      "unknown_outcome_alerts": int((~picked["known"]).sum()),
                      "recorded_hits_per_alert": hits/picked.height})
    alerts = sum(f["alerts"] for f in folds)
    hits = sum(f["recorded_hits"] for f in folds)
    out = {"method": "history_rule_v1", "target": "recorded guarded SMVU alarm next day",
           "note": "explored 2025 blocks, not blind; unknown outcomes included in denominator",
           "folds": folds, "pooled": {"alerts": alerts, "recorded_hits": hits,
                                      "unknown_outcome_alerts": sum(f["unknown_outcome_alerts"] for f in folds),
                                      "recorded_hits_per_alert": hits/alerts}}
    path = PATHS.reports / "guard_review_queue_backtest.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
