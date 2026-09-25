"""Evaluate the frozen weekly guard pilot only after complete new windows arrive.

The default boundary is after all previously inspected data. This script never
chooses a threshold, retrains a model, or treats an unobserved week as a miss.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter, defaultdict

import polars as pl

from mkl import guard_queue, guard_weekly, store
from mkl.config import PATHS

FROZEN_START = dt.date(2026, 7, 1)
FROZEN_POLICY = (4, 14, 4)


def last_complete_monday(last_observed_day: dt.date) -> dt.date:
    """D+8 must be observed before a Monday recommendation is scored."""
    last_asof = last_observed_day - dt.timedelta(days=8)
    return last_asof - dt.timedelta(days=last_asof.weekday())


def evaluate(start: dt.date = FROZEN_START, end: dt.date | None = None) -> dict:
    if (guard_weekly.MIN_ALARM_DAYS, guard_weekly.COOLDOWN_DAYS,
            guard_weekly.BUDGET) != FROZEN_POLICY:
        raise ValueError("weekly pilot policy changed; start a new evaluation version")
    ready = guard_weekly.readiness(require_recent=False)
    if ready["status"] != "ready":
        raise ValueError(f"weekly pilot data are {ready['status']}; refresh first")
    last_day = dt.date.fromisoformat(ready["data_last_day"])
    complete = last_complete_monday(last_day)
    last_asof = min(complete, end) if end else complete
    result = {
        "policy": {"model_version": guard_weekly.METHOD,
                   "min_alarm_days_in_last_7": FROZEN_POLICY[0],
                   "cooldown_days": FROZEN_POLICY[1],
                   "budget_per_week": FROZEN_POLICY[2]},
        "target": "continued_recorded_smvu_guard_alarm_activity",
        "target_window": "D+2..D+8 inclusive",
        "evaluation_start": start.isoformat(),
        "evaluation_end": last_asof.isoformat(),
        "last_observed_day": last_day.isoformat(),
        "last_complete_monday": complete.isoformat(),
        "event_cache_through": ready["event_cache_through"],
        "status": "awaiting_new_complete_week",
        "selection_count": 0,
    }
    if last_asof < start:
        return result

    frame = store.read_slice("object", guard_weekly.FIRST_REPLAY_DAY,
                             last_asof, columns=["obj", "day", "obj_armed",
                                                  "days_since_arm_event"])
    events = pl.read_parquet(guard_queue.EVENT_DAYS).filter(pl.col("day") <= last_day)
    selected, _ = guard_weekly.replay(frame, events, last_asof)
    selected = [row for row in selected if start <= row["day"] <= last_asof]
    positive = defaultdict(set)
    unresolved = set()
    for obj, day, hit, unknown in events.select(
            "obj", "day", "positive", "unresolved").iter_rows():
        obj = str(obj)
        if hit:
            positive[obj].add(day)
        if unknown:
            unresolved.add((obj, day))
    observed = {(str(obj), day) for obj, day in pl.scan_parquet(
        PATHS.features / "object.parquet").filter(
            pl.col("day").is_between(start + dt.timedelta(days=2),
                                     last_asof + dt.timedelta(days=8)))
        .select("obj", "day").collect().iter_rows()}

    def outcome(obj: str, asof: dt.date) -> str:
        days = [asof + dt.timedelta(days=offset) for offset in range(2, 9)]
        if any(day in positive[obj] for day in days):
            return "hit"
        if any((obj, day) not in observed or (obj, day) in unresolved
               for day in days):
            return "unknown"
        return "miss"

    picks = [{"asof": row["day"].isoformat(), "obj": str(row["obj"]),
              "outcome": outcome(str(row["obj"]), row["day"])}
             for row in selected]
    candidate = guard_queue.current_candidates(frame).filter(
        (pl.col("day").dt.weekday() == 1) &
        pl.col("day").is_between(start, last_asof))
    candidate_outcomes = Counter(outcome(str(obj), day) for obj, day in
                                 candidate.select("obj", "day").iter_rows())
    counts = Counter(row["outcome"] for row in picks)
    by_obj = Counter(row["obj"] for row in picks)
    result.update({
        "status": "evaluated",
        "selection_count": len(picks),
        "hits": counts["hit"], "misses": counts["miss"],
        "unknown": counts["unknown"],
        "precision_lower_bound": counts["hit"] / len(picks) if picks else None,
        "precision_known_only": (counts["hit"] / (counts["hit"] + counts["miss"])
                                 if counts["hit"] + counts["miss"] else None),
        "eligible_candidate_weeks": sum(candidate_outcomes.values()),
        "eligible_positive_weeks": candidate_outcomes["hit"],
        "eligible_unknown_weeks": candidate_outcomes["unknown"],
        "recorded_recall": (counts["hit"] / candidate_outcomes["hit"]
                            if candidate_outcomes["hit"] else None),
        "unique_recommended_objects": len(by_obj),
        "top_three_object_share": (sum(n for _, n in by_obj.most_common(3)) /
                                   len(picks) if picks else None),
        "recommendations": picks,
    })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=dt.date.fromisoformat,
                        default=FROZEN_START)
    parser.add_argument("--end", type=dt.date.fromisoformat)
    parser.add_argument("--output", help="optional JSON output path")
    args = parser.parse_args()
    result = evaluate(args.start, args.end)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
