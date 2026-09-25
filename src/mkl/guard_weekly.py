"""Weekly manual inspection watchlist for recurrent guarded SMVU alarms.

Every Monday after the day closes, rank objects that had guarded alarm signals
on at least four of the previous seven days. The forecast window is Wednesday
through next Tuesday (D+2..D+8): at least a full day separates calculation
from the first target day. This predicts *continued recorded alarm activity*,
not a new intrusion. A deterministic 14-day cooldown avoids repeated new
recommendations for the same object. No future outcomes enter selection.
"""
from __future__ import annotations

import datetime as dt
import json

import polars as pl

from . import address, contract, guard_queue, store
from .config import PATHS

FIRST_REPLAY_DAY = dt.date(2023, 2, 1)
MIN_ALARM_DAYS = 4
COOLDOWN_DAYS = 14
BUDGET = 4
SCHEMA_VERSION = "1.0"
METHOD = "weekly_recurrence_rule_v1"


def replay(frame: pl.DataFrame, event_days: pl.DataFrame,
           asof: dt.date) -> tuple[list[dict], dict]:
    """Reconstruct past weekly selections so cooldown needs no hidden state."""
    if asof.weekday() != 0:
        raise ValueError("weekly inspection calculation requires Monday asof")
    current = frame.filter(pl.col("day") == asof)
    if current.is_empty():
        raise ValueError(f"no object feature rows on {asof}")
    if current["obj"].null_count() or current["obj"].n_unique() != current.height:
        raise ValueError(f"object feature rows on {asof} need unique non-null obj")
    monday = frame.filter((pl.col("day") <= asof) &
                          (pl.col("day").dt.weekday() == 1))
    if monday["obj"].null_count() or monday.select("obj", "day").unique().height != monday.height:
        raise ValueError("weekly replay requires unique non-null object-day rows")
    candidate = guard_queue.current_candidates(monday)
    scored = guard_queue.priority_score(
        guard_queue.add_alarm_history(candidate, event_days)).sort("day")
    last_selected: dict[str, dt.date] = {}
    selected = []
    today_eligible = today_meeting = today_suppressed = 0
    for day_frame in scored.partition_by("day", maintain_order=True):
        day = day_frame["day"][0]
        today = day == asof
        if today:
            today_eligible = day_frame.height
        chosen = 0
        for row in day_frame.sort(["priority_score", "obj"],
                                  descending=[True, False]).to_dicts():
            if row["exact_count_7"] < MIN_ALARM_DAYS:
                continue
            obj = str(row["obj"])
            if today:
                today_meeting += 1
            if obj in last_selected and (day-last_selected[obj]).days <= COOLDOWN_DAYS:
                if today:
                    today_suppressed += 1
                continue
            if chosen >= BUDGET:
                continue
            last_selected[obj] = day
            selected.append(row)
            chosen += 1
    current_picks = [row for row in selected if row["day"] == asof]
    return selected, {"objects_with_features": current.height,
                      "eligible_objects": today_eligible,
                      "meeting_alarm_threshold": today_meeting,
                      "suppressed_by_cooldown": today_suppressed,
                      "returned": len(current_picks)}


def weekly_inspections(asof: dt.date | None = None) -> dict:
    """Return a small scheduled watchlist; refuse missing or stale v2 cache."""
    ready = readiness()
    if ready["status"] != "ready":
        if ready["status"] == "missing_data":
            missing = ", ".join(ready.get("missing", []))
            raise FileNotFoundError(
                f"weekly queue data missing ({missing}); rebuild the data cache")
        raise ValueError(
            f"weekly queue is {ready['status']}; refresh the data cache before serving")
    info = json.loads(guard_queue.BUILD_INFO.read_text(encoding="utf-8"))
    last_day = pl.scan_parquet(PATHS.features / "object.parquet").select(
        pl.col("day").max()).collect().item()
    day = asof or last_day-dt.timedelta(days=last_day.weekday())
    guard_queue.validate_cache_day(day, last_day, info)
    if day < FIRST_REPLAY_DAY:
        raise ValueError(f"weekly replay begins on {FIRST_REPLAY_DAY}")
    frame = store.read_slice("object", FIRST_REPLAY_DAY, day, columns=[
        "obj", "day", "obj_armed", "days_since_arm_event"])
    events = pl.read_parquet(guard_queue.EVENT_DAYS).filter(pl.col("day") <= day)
    selected, counts = replay(frame, events, day)
    rows = []
    for rank, row in enumerate((r for r in selected if r["day"] == day), 1):
        obj = str(row["obj"])
        addr = address.describe(obj=obj)
        rows.append({"obj": obj, "rank": rank,
                     "recommendation_id": contract.make_alert_id(
                         "guard_weekly", {"obj": obj, "target": "D+2..D+8"}, day),
                     "case_key": contract.make_case_key("guard_weekly", {"obj": obj}),
                     "priority_score": float(row["priority_score"]),
                     "recent_alarm_days_7": int(row["exact_count_7"]),
                     "recent_alarm_days_30": int(row["exact_count_30"]),
                     "guard_state_age_days": int(row["days_since_arm_event"]),
                     "evidence": "alarm_on_at_least_4_of_previous_7_days",
                     "obj_name": addr.get("obj_name"),
                     "obj_parent_name": addr.get("obj_parent_name"),
                     "address_known": addr.get("address_known", False)})
    cache_through = (dt.date.fromisoformat(info["end"]) +
                     dt.timedelta(days=1)).isoformat()
    return {"schema_version": SCHEMA_VERSION,
            "asof": day.isoformat(),
            "valid_from": (day+dt.timedelta(days=2)).isoformat(),
            "valid_to": (day+dt.timedelta(days=9)).isoformat(),
            "next_run": (day+dt.timedelta(days=7)).isoformat(),
            "target": "continued_recorded_smvu_guard_alarm_activity",
            "target_version": 2,
            "model_version": METHOD,
            "action": "manual_plan_guard_loop_inspection",
            "score_type": "relative_priority_not_probability",
            "method": METHOD,
            "result_status": "ok" if rows else "empty_valid",
            "policy": {"alarm_days_in_last_7_at_least": MIN_ALARM_DAYS,
                       "max_objects_per_week": BUDGET,
                       "same_object_cooldown_days": COOLDOWN_DAYS},
            **counts, "priorities": rows,
            "event_cache_through": cache_through,
            "data_snapshot": {"object_features_through": last_day.isoformat(),
                              "event_cache_through": cache_through,
                              "event_cache_version": info.get("version"),
                              "label_build_end": info.get("end")},
            "data_last_day": last_day.isoformat()}


def readiness() -> dict:
    """Readiness of the weekly manual queue, independent of legacy models."""
    required = {
        "object_features": PATHS.features / "object.parquet",
        "event_days": guard_queue.EVENT_DAYS,
        "event_build_info": guard_queue.BUILD_INFO,
        "channel_catalog": PATHS.interim / "channels.parquet",
    }
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        return {"status": "missing_data", "scenario": METHOD,
                "missing": missing}
    try:
        info = json.loads(guard_queue.BUILD_INFO.read_text(encoding="utf-8"))
        last_day = pl.scan_parquet(PATHS.features / "object.parquet").select(
            pl.col("day").max()).collect().item()
        cache_through = (dt.date.fromisoformat(info["end"]) +
                         dt.timedelta(days=1))
    except (OSError, ValueError, KeyError) as exc:
        return {"status": "error", "scenario": METHOD,
                "detail": f"cannot inspect data freshness: {exc}"}
    status = "ready"
    if info.get("version") != 2 or last_day > cache_through:
        status = "stale"
    return {"status": status, "scenario": METHOD,
            "schema_version": SCHEMA_VERSION, "model_version": METHOD,
            "data_last_day": last_day.isoformat(),
            "event_cache_through": cache_through.isoformat(),
            "event_cache_version": info.get("version"),
            "missing": []}
