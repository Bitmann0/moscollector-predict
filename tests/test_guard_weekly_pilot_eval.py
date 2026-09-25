import datetime as dt

from scripts.evaluate_guard_weekly_pilot import evaluate, last_complete_monday


def test_last_complete_monday_leaves_full_target_window():
    assert last_complete_monday(dt.date(2026, 6, 30)) == dt.date(2026, 6, 22)
    assert last_complete_monday(dt.date(2026, 7, 8)) == dt.date(2026, 6, 29)


def test_pilot_evaluator_waits_for_new_complete_week(monkeypatch):
    monkeypatch.setattr(
        "scripts.evaluate_guard_weekly_pilot.guard_weekly.readiness",
        lambda: {"status": "ready", "data_last_day": "2026-06-30",
                 "event_cache_through": "2026-06-30"},
    )
    result = evaluate()
    assert result["status"] == "awaiting_new_complete_week"
    assert result["selection_count"] == 0
    assert result["last_complete_monday"] == "2026-06-22"
