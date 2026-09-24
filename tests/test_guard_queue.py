"""Decision-time and contract checks for the manual guarded-alarm queue."""
import datetime as dt

import polars as pl
import pytest
from fastapi.testclient import TestClient

from mkl import api, guard_queue


DAY = dt.date(2025, 4, 5)


def _base():
    return pl.DataFrame({
        "obj": ["B", "A", "C", "D"], "day": [DAY] * 4,
        "obj_armed": [1, 1, 0, 1],
        "days_since_arm_event": [2, 0, 0, 8],
    })


def _events(extra=False):
    rows = [("A", DAY-dt.timedelta(days=3), True),
            ("B", DAY-dt.timedelta(days=20), True)]
    if extra:
        rows.append(("B", DAY+dt.timedelta(days=1), True))
    return pl.DataFrame(rows, schema=["obj", "day", "positive"], orient="row")


def test_queue_uses_current_guard_and_only_past_alarm_history():
    first = guard_queue.rank_frame(_base(), _events(), DAY)
    future = guard_queue.rank_frame(_base(), _events(extra=True), DAY)
    assert first == future
    assert first["eligible_objects"] == 2
    assert first["excluded_disarmed"] == 1
    assert first["excluded_stale_guard"] == 1
    assert [r["obj"] for r in first["priorities"]] == ["A", "B"]
    assert first["priorities"][0]["recent_alarm_days_7"] == 1
    assert first["priorities"][1]["recent_alarm_days_7"] == 0
    assert first["score_type"] == "relative_priority_not_probability"
    assert first["action"] == "manual_review_only"
    assert first["valid_from"] == "2025-04-06"


def test_queue_never_claims_more_than_validated_budget():
    with pytest.raises(ValueError, match="1 or 4"):
        guard_queue.rank_frame(_base(), _events(), DAY, budget=2)
    assert guard_queue.rank_frame(_base(), _events(), DAY, budget=1)["returned"] == 1


def test_cache_must_include_the_scored_day_after_new_data_arrives():
    info = {"version": 2, "start": "2023-01-01", "end": "2025-04-04"}
    guard_queue.validate_cache_day(DAY, DAY, info)
    with pytest.raises(ValueError, match="stale"):
        guard_queue.validate_cache_day(DAY, DAY, {**info, "end": "2025-04-03"})
    with pytest.raises(ValueError, match="outside available"):
        guard_queue.validate_cache_day(DAY+dt.timedelta(days=1), DAY, info)


def test_duplicate_object_rows_do_not_create_duplicate_recommendations():
    duplicate = pl.concat([_base(), _base().head(1)])
    with pytest.raises(ValueError, match="unique non-null obj"):
        guard_queue.rank_frame(duplicate, _events(), DAY)


def test_api_keeps_queue_separate_from_legacy_alerts_and_orders(monkeypatch):
    monkeypatch.setattr(api.guard_queue, "daily_priorities",
                        lambda asof=None, budget=4: guard_queue.rank_frame(
                            _base(), _events(), asof or DAY, budget))
    client = TestClient(api.app)
    got = client.get("/api/v1/guard-signal-priorities?asof=2025-04-05").json()
    assert got["method"] == "history_rule_v1"
    assert got["priorities"][0]["obj"] == "A"
    assert "work_order_id" not in got["priorities"][0]
