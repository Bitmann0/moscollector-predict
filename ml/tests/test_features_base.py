import duckdb
import pytest

from conftest import insert_day
from mkl.features import base


@pytest.fixture
def con(daily_con):
    return daily_con


def _day(c, ch, day, n_events=1, n_alarms=0, n_bad=0, max_gap_s=0):
    insert_day(c, ch=ch, day=day, n_events=n_events, n_alarms=n_alarms,
               n_bad=n_bad, max_gap_s=max_gap_s)


def test_rolling_window_excludes_future_days(con):
    _day(con, 1, "2025-01-01", n_events=1)
    _day(con, 1, "2025-01-02", n_events=10)
    base.add_rolling_windows(con, windows=(7,))
    v = con.execute(
        "SELECT n_events_w7 FROM feat_base WHERE day = DATE '2025-01-01'"
    ).fetchone()[0]
    assert v == 1, "окно на 1 января не должно видеть 2 января"


def test_rolling_window_accumulates_past(con):
    _day(con, 1, "2025-01-01", n_events=1)
    _day(con, 1, "2025-01-02", n_events=10)
    base.add_rolling_windows(con, windows=(7,))
    v = con.execute(
        "SELECT n_events_w7 FROM feat_base WHERE day = DATE '2025-01-02'"
    ).fetchone()[0]
    assert v == 11


def test_rolling_window_drops_events_older_than_window(con):
    _day(con, 1, "2025-01-01", n_events=100)
    _day(con, 1, "2025-01-20", n_events=1)
    base.add_rolling_windows(con, windows=(7,))
    v = con.execute(
        "SELECT n_events_w7 FROM feat_base WHERE day = DATE '2025-01-20'"
    ).fetchone()[0]
    assert v == 1


def test_days_since_last_alarm(con):
    _day(con, 1, "2025-01-01", n_alarms=1)
    _day(con, 1, "2025-01-02")
    _day(con, 1, "2025-01-05")
    base.add_rolling_windows(con, windows=(7,))
    got = {str(d): v for d, v in con.execute(
        "SELECT day, days_since_last_alarm FROM feat_base ORDER BY day").fetchall()}
    assert got["2025-01-01"] == 0
    assert got["2025-01-02"] == 1
    assert got["2025-01-05"] == 4


def test_pareto_rank_orders_channels_within_object(con):
    _day(con, 1, "2025-01-01", n_alarms=100)
    _day(con, 2, "2025-01-01", n_alarms=1)
    base.add_rolling_windows(con, windows=(7,))
    got = dict(con.execute("SELECT ch, pareto_rank_obj FROM feat_base").fetchall())
    assert got[1] < got[2]


def test_chatter_and_alarm_rates_are_normalised(con):
    _day(con, 1, "2025-01-01", n_events=10, n_alarms=3)
    con.execute("UPDATE daily_channel SET n_chatter_1min = 5")
    base.add_rolling_windows(con, windows=(7,))
    row = con.execute("SELECT chatter_rate, alarm_rate FROM feat_base").fetchone()
    assert row[0] == pytest.approx(0.5)
    assert row[1] == pytest.approx(0.3)


def test_silence_ratio_flags_unusually_long_gap(con):
    for d in range(1, 10):
        _day(con, 1, f"2025-01-{d:02d}", max_gap_s=100)
    _day(con, 1, "2025-01-10", max_gap_s=10000)
    base.add_rolling_windows(con, windows=(7,))
    got = {str(d): v for d, v in con.execute(
        "SELECT day, silence_z FROM feat_base ORDER BY day").fetchall()}
    assert got["2025-01-10"] > 2.0
