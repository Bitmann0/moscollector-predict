"""Признаки-предвестники: отношение текущей активности к собственной базовой линии.

Аудит 250 реальных отказов показал, что перед отказом активность канала чаще
растёт, чем падает (135 против 4 случаев), поэтому ускорение задано явным
признаком, а не оставлено на откуп модели.
"""
import duckdb
import pytest

from conftest import insert_day
from mkl.features import base


@pytest.fixture
def con(daily_con):
    return daily_con


def _day(c, day, n_events=1, n_alarms=0, ch=1):
    insert_day(c, ch=ch, day=day, n_events=n_events, n_alarms=n_alarms)


def test_events_accel_detects_activity_spike(con):
    for d in range(1, 25):
        _day(con, f"2025-01-{d:02d}", n_events=10)
    for d in range(25, 31):
        _day(con, f"2025-01-{d:02d}", n_events=100)
    base.add_rolling_windows(con, windows=(7, 30))
    accel = dict(con.execute(
        "SELECT day, events_accel FROM feat_base ORDER BY day"
    ).fetchall())
    quiet = accel[__import__("datetime").date(2025, 1, 20)]
    spike = accel[__import__("datetime").date(2025, 1, 30)]
    assert spike > 2.0 * quiet


def test_events_vs_own_baseline_is_one_when_stable(con):
    for d in range(1, 31):
        _day(con, f"2025-01-{d:02d}", n_events=10)
    base.add_rolling_windows(con, windows=(7, 30))
    v = con.execute(
        "SELECT events_vs_own_w30 FROM feat_base WHERE day = DATE '2025-01-30'"
    ).fetchone()[0]
    assert v == pytest.approx(1.0)


def test_accel_features_absent_without_both_windows(con):
    _day(con, "2025-01-01")
    base.add_rolling_windows(con, windows=(7,))
    cols = {r[0] for r in con.execute("DESCRIBE feat_base").fetchall()}
    assert "events_accel" not in cols


def test_accel_features_present_with_both_windows(con):
    _day(con, "2025-01-01")
    base.add_rolling_windows(con, windows=(7, 30))
    cols = {r[0] for r in con.execute("DESCRIBE feat_base").fetchall()}
    assert {"events_accel", "alarms_accel", "bad_accel", "events_vs_own_w30",
            "events_z_own_w30", "activity_days_ratio", "gap_vs_own_rhythm"} <= cols


def test_prev_gap_days_tracks_reporting_rhythm(con):
    for d in (1, 2, 3, 8):
        _day(con, f"2025-01-{d:02d}")
    base.add_rolling_windows(con, windows=(7, 30))
    got = {str(d): v for d, v in con.execute(
        "SELECT day, prev_gap_days FROM feat_base ORDER BY day").fetchall()}
    assert got["2025-01-02"] == 1
    assert got["2025-01-08"] == 5


def test_gap_vs_own_rhythm_flags_unusual_gap(con):
    for d in range(1, 21):
        _day(con, f"2025-01-{d:02d}")
    _day(con, "2025-01-28")
    base.add_rolling_windows(con, windows=(7, 30))
    got = {str(d): v for d, v in con.execute(
        "SELECT day, gap_vs_own_rhythm FROM feat_base ORDER BY day").fetchall()}
    assert got["2025-01-28"] > 2.0, "разрыв в 8 суток при ежедневном ритме — аномалия"
