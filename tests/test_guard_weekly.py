"""Weekly guard-loop watchlist: lead time, abstention, and cooldown."""
import datetime as dt

import polars as pl
import pytest
from fastapi.testclient import TestClient

from mkl import api, guard_weekly


MONDAY = dt.date(2025, 4, 7)


def _data():
    mondays = [MONDAY+dt.timedelta(days=7*i) for i in range(4)]
    frame = pl.DataFrame({"obj": ["A", "B"]*4,
                          "day": [day for day in mondays for _ in range(2)],
                          "obj_armed": [1]*8,
                          "days_since_arm_event": [0]*8})
    days = [day+dt.timedelta(days=offset)
            for day in mondays for offset in (-6, -5, -4, -3)]
    events = pl.DataFrame({"obj": ["A"]*len(days),
                           "day": days, "positive": [True]*len(days)})
    return frame, events, mondays


def test_weekly_replay_avoids_duplicate_recommendations_for_14_days():
    frame, events, mondays = _data()
    selected, counts = guard_weekly.replay(frame, events, mondays[-1])
    shuffled, _ = guard_weekly.replay(frame.sample(fraction=1, shuffle=True, seed=7),
                                      events, mondays[-1])
    assert [(r["obj"], r["day"]) for r in selected] == [
        ("A", mondays[0]), ("A", mondays[3])]
    assert [(r["obj"], r["day"]) for r in shuffled] == [
        ("A", mondays[0]), ("A", mondays[3])]
    assert counts["meeting_alarm_threshold"] == 1
    assert counts["returned"] == 1
    assert all(r["exact_count_7"] == 4 for r in selected)


def test_weekly_replay_abstains_without_four_alarm_days():
    frame, events, mondays = _data()
    events = events.filter(pl.col("day") >= mondays[-1]-dt.timedelta(days=3))
    selected, counts = guard_weekly.replay(frame, events, mondays[0])
    assert selected == []
    assert counts["returned"] == 0
    with pytest.raises(ValueError, match="Monday"):
        guard_weekly.replay(frame, events, mondays[0]+dt.timedelta(days=1))


def test_duplicate_historical_object_day_is_rejected():
    frame, events, mondays = _data()
    with pytest.raises(ValueError, match="unique non-null object-day"):
        guard_weekly.replay(pl.concat([frame, frame.head(1)]), events, mondays[-1])


def test_weekly_api_is_separate_from_automatic_work_orders(monkeypatch):
    monkeypatch.setattr(api.guard_weekly, "weekly_inspections", lambda asof=None: {
        "asof": "2025-04-07", "valid_from": "2025-04-09",
        "valid_to": "2025-04-16", "action": "manual_plan_guard_loop_inspection",
        "priorities": [{"obj": "A", "rank": 1}]})
    got = TestClient(api.app).get("/api/v1/guard-weekly-inspections").json()
    assert got["valid_from"] == "2025-04-09"
    assert got["action"] == "manual_plan_guard_loop_inspection"
    assert "work_order_id" not in got["priorities"][0]
