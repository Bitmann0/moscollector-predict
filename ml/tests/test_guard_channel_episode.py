"""The experimental channel history must not read future alarm events."""
import datetime as dt
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from exp_guard_channel_episode import add_channel_history  # noqa: E402


def test_channel_history_uses_only_asof_events_and_calendar_windows():
    monday = dt.date(2025, 4, 7)
    frame = pl.DataFrame({"obj": ["A", "A"],
                          "day": [monday, monday + dt.timedelta(days=7)]})
    alarms = {"A": [
        (monday - dt.timedelta(days=30), "outside", 1),
        (monday - dt.timedelta(days=29), "prior", 2),
        (monday - dt.timedelta(days=6), "recent", 3),
        (monday + dt.timedelta(days=1), "future", 100),
    ]}
    got, _ = add_channel_history(frame, alarms)
    first, second = got.to_dicts()
    assert first["channel_count_7"] == 1
    assert first["channel_count_30"] == 2
    assert first["channel_events_30"] == 5
    assert first["channel_new_7"] == 1
    assert second["channel_count_7"] == 1
    assert second["channel_count_30"] == 2
    assert second["channel_events_30"] == 103
