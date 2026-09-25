"""Explore abstaining daily and non-overlapping weekly guarded-alarm reviews.

Diagnostic only: explored calendar periods and proxy labels. Every candidate
is selected using current-day guard state; unknown future observability remains
in the alert denominator.
"""
import bisect
import datetime as dt
import json
from collections import defaultdict

import polars as pl

from mkl import guard_queue, store
from mkl.config import PATHS

START, END = dt.date(2025, 1, 1), dt.date(2026, 6, 22)


def main() -> None:
    base = store.read_slice("object", START, END, columns=[
        "obj", "day", "obj_armed", "days_since_arm_event"])
    events = pl.read_parquet(guard_queue.EVENT_DAYS)
    feature = guard_queue.priority_score(guard_queue.add_alarm_history(
        guard_queue.current_candidates(base), events)).sort(
            ["day", "priority_score", "obj"], descending=[False, True, False])
    positive: dict[str, list[dt.date]] = defaultdict(list)
    unresolved = set()
    for obj, day, pos, unk in events.select(
            "obj", "day", "positive", "unresolved").iter_rows():
        if pos:
            positive[str(obj)].append(day)
        if unk:
            unresolved.add((str(obj), day))
    for days in positive.values():
        days.sort()
    observed = {(str(obj), day) for obj, day in pl.read_parquet(
        PATHS.features / "object.parquet", columns=["obj", "day"]).iter_rows()}

    def outcome(obj, day, first, last):
        days = positive[obj]
        hit = bisect.bisect_right(days, day+dt.timedelta(days=last)) > bisect.bisect_left(
            days, day+dt.timedelta(days=first))
        known = hit or all((obj, day+dt.timedelta(days=i)) in observed and
                           (obj, day+dt.timedelta(days=i)) not in unresolved
                           for i in range(first, last+1))
        return hit, known

    report = {"note": "exploratory; recorded SMVU signals, not incidents; old periods",
              "policies": []}
    periods = ((dt.date(2025, 1, 1), dt.date(2025, 6, 30)),
               (dt.date(2025, 7, 1), dt.date(2025, 12, 31)),
               (dt.date(2026, 1, 1), dt.date(2026, 6, 22)))
    for schedule, first, last, budget in (("daily", 1, 1, 4),
                                          ("weekly_monday", 2, 8, 4)):
        for min_week in (0, 1, 2, 3, 4):
            by_period = []
            for start, end in periods:
                block = feature.filter(pl.col("day").is_between(start, end))
                if schedule == "weekly_monday":
                    block = block.filter(pl.col("day").dt.weekday() == 1)
                picks = []
                for day in block.partition_by("day", maintain_order=True):
                    picks.extend(day.filter(pl.col("exact_count_7") >= min_week)
                                 .head(budget).select("obj", "day").iter_rows())
                outcomes = [outcome(str(obj), day, first, last)
                            for obj, day in picks]
                hits = sum(hit for hit, _ in outcomes)
                alerts = len(outcomes)
                by_period.append({"start": str(start), "end": str(end),
                                  "alerts": alerts, "recorded_hits": hits,
                                  "unknown": sum(not known for _, known in outcomes),
                                  "recorded_hit_rate": hits/alerts if alerts else None})
            n, h = sum(x["alerts"] for x in by_period), sum(x["recorded_hits"] for x in by_period)
            report["policies"].append({"schedule": schedule,
                "window_days_after_asof": [first, last], "budget": budget,
                "min_alarm_days_in_last_7": min_week,
                "periods": by_period,
                "pooled": {"alerts": n, "recorded_hits": h,
                           "unknown": sum(x["unknown"] for x in by_period),
                           "recorded_hit_rate": h/n if n else None}})
    out = PATHS.reports / "guard_longer_horizon_exploration.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in report["policies"]:
        print(row["schedule"], row["min_alarm_days_in_last_7"], row["pooled"],
              [(x["alerts"], x["recorded_hits"]) for x in row["periods"]])


if __name__ == "__main__":
    main()
