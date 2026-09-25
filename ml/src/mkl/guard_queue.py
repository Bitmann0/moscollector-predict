"""Manual review queue for next-day *recorded* guarded SMVU alarm risk.

The priority is an ordinal history rule validated on the full daily queue.
It is deliberately not a calibrated incident probability or a work order.
Only data available by the end of ``asof`` may affect candidate selection or
ranking. The event-day cache must be rebuilt when new raw days arrive.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
from collections import defaultdict

import polars as pl

from . import address, store
from .config import PATHS

EVENT_DAYS = PATHS.features / "intrusion_eventtime_days_v2.parquet"
BUILD_INFO = PATHS.reports / "intrusion_eventtime_v2_build.json"
DEFAULT_BUDGET = 4


def current_candidates(frame: pl.DataFrame) -> pl.DataFrame:
    """The same eligibility as the operational backtest, using only today."""
    return frame.filter((pl.col("obj_armed") == 1) &
                        (pl.col("days_since_arm_event") <= 7))


def add_alarm_history(frame: pl.DataFrame, event_days: pl.DataFrame) -> pl.DataFrame:
    """Four reproducible history features, never reading a future event day."""
    required = {"obj", "day"}
    if not required <= set(frame.columns) or not required | {"positive"} <= set(event_days.columns):
        raise ValueError("object rows and event days require obj/day/positive columns")
    by_obj: dict[str, list[dt.date]] = defaultdict(list)
    for obj, day in event_days.filter(pl.col("positive") == True).select(
            "obj", "day").unique().iter_rows():
        by_obj[str(obj)].append(day)
    for dates in by_obj.values():
        dates.sort()

    rows = []
    for obj, day in frame.select("obj", "day").iter_rows():
        dates = by_obj[str(obj)]
        right = bisect.bisect_right(dates, day)
        counts = [right-bisect.bisect_left(dates, day-dt.timedelta(days=w-1))
                  for w in (1, 7, 30)]
        age = min((day-dates[right-1]).days, 366) if right else 366
        rows.append((*counts, age))
    history = pl.DataFrame(rows, schema=["exact_count_1", "exact_count_7",
                                         "exact_count_30", "exact_age"],
                           orient="row")
    return frame.hstack(history)


def priority_score(frame: pl.DataFrame) -> pl.DataFrame:
    """Week count dominates month count; age only breaks remaining ties."""
    return frame.with_columns((pl.col("exact_count_7") * 32 +
                               pl.col("exact_count_30") / 32 +
                               1 / (1024 * (pl.col("exact_age") + 1)))
                              .cast(pl.Float64).alias("priority_score"))


def rank_frame(frame: pl.DataFrame, event_days: pl.DataFrame,
               asof: dt.date, budget: int = DEFAULT_BUDGET) -> dict:
    """Pure ranking step; future labels are neither inputs nor filters."""
    if budget not in (1, 4):
        raise ValueError("validated budget is 1 or 4 objects per day")
    current = frame.filter(pl.col("day") == asof)
    if current.is_empty():
        raise ValueError(f"no object feature rows on {asof}")
    if current["obj"].null_count() or current["obj"].n_unique() != current.height:
        raise ValueError(f"object feature rows on {asof} need unique non-null obj")
    candidates = current_candidates(current)
    not_armed = current.filter(pl.col("obj_armed") == 0).height
    unknown_guard = current.filter(pl.col("obj_armed").is_null()).height
    stale_guard = current.height - candidates.height - not_armed - unknown_guard
    scored = priority_score(add_alarm_history(candidates, event_days)).sort(
        ["priority_score", "obj"], descending=[True, False])
    selected = scored.head(budget)
    start = asof + dt.timedelta(days=1)
    rows = []
    for rank, row in enumerate(selected.to_dicts(), 1):
        week = int(row["exact_count_7"])
        month = int(row["exact_count_30"])
        age = int(row["exact_age"])
        rows.append({"obj": str(row["obj"]), "rank": rank,
                     "priority_score": float(row["priority_score"]),
                     "recent_alarm_days_7": week,
                     "recent_alarm_days_30": month,
                     "days_since_last_alarm": None if age == 366 else age,
                     "guard_state_age_days": int(row["days_since_arm_event"]),
                     "evidence": ("alarm_in_last_7_days" if week else
                                  "alarm_in_last_30_days" if month else
                                  "no_recorded_alarm_in_last_30_days")})
    return {"asof": asof.isoformat(), "valid_from": start.isoformat(),
            "valid_to": (start+dt.timedelta(days=1)).isoformat(),
            "target": "recorded_smvu_guard_alarm_next_day",
            "target_version": 2, "action": "manual_review_only",
            "score_type": "relative_priority_not_probability",
            "method": "history_rule_v1", "budget": budget,
            "objects_with_features": current.height,
            "eligible_objects": candidates.height,
            "excluded_disarmed": not_armed,
            "excluded_unknown_guard": unknown_guard,
            "excluded_stale_guard": stale_guard,
            "returned": len(rows), "priorities": rows}


def validate_cache_day(asof: dt.date, last_day: dt.date, info: dict) -> None:
    """Reject stale labels instead of ranking new days with old alarm history."""
    if info.get("version") != 2:
        raise ValueError("event-time cache is not v2")
    if asof > last_day or asof < dt.date.fromisoformat(info["start"]):
        raise ValueError(f"requested day {asof} outside available object history")
    if asof > dt.date.fromisoformat(info["end"]) + dt.timedelta(days=1):
        raise ValueError("event-time cache is stale for this day; rebuild labels")


def daily_priorities(asof: dt.date | None = None,
                     budget: int = DEFAULT_BUDGET) -> dict:
    """Load one day's queue; refuse a stale event cache after new data arrive."""
    if not EVENT_DAYS.exists() or not BUILD_INFO.exists():
        raise FileNotFoundError("event-time cache missing; run build_intrusion_eventtime_labels.py")
    info = json.loads(BUILD_INFO.read_text(encoding="utf-8"))
    last_day = pl.scan_parquet(PATHS.features / "object.parquet").select(
        pl.col("day").max()).collect().item()
    day = asof or last_day
    validate_cache_day(day, last_day, info)
    base = store.read_slice("object", day, day)
    events = pl.read_parquet(EVENT_DAYS).filter(pl.col("day") <= day)
    result = rank_frame(base, events, day, budget)
    for row in result["priorities"]:
        addr = address.describe(obj=row["obj"])
        row["obj_name"] = addr.get("obj_name")
        row["obj_parent_name"] = addr.get("obj_parent_name")
        row["address_known"] = addr.get("address_known", False)
    result["event_cache_through"] = (dt.date.fromisoformat(info["end"]) +
                                      dt.timedelta(days=1)).isoformat()
    result["data_last_day"] = last_day.isoformat()
    return result
