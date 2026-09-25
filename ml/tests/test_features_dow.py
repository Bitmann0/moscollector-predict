"""Профиль отчётности канала по дням недели.

Пропуск понедельника у канала, который по понедельникам никогда и не
отчитывался, — норма, а не отказ; голый признак дня недели этого не различает.
"""
import duckdb
import pytest

from mkl.features import lifecycle


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE feat_ephist (ch BIGINT, day DATE, n_events BIGINT)")
    yield c
    c.close()


def _day(c, day, ch=1):
    c.execute("INSERT INTO feat_ephist VALUES (?, ?, 1)", [ch, day])


def test_daily_channel_has_full_weekday_coverage(con):
    # 2025-01-06 — понедельник; берём восемь недель подряд ежедневно
    for d in range(6, 62):
        con.execute("INSERT INTO feat_ephist VALUES (1, DATE '2025-01-06' + ?, 1)",
                    [d - 6])
    lifecycle.add_weekday_profile(con)
    v = con.execute(
        "SELECT dow_active_rate FROM feat_dow ORDER BY day DESC LIMIT 1"
    ).fetchone()[0]
    assert v == pytest.approx(1.0, abs=0.15)


def test_weekday_only_channel_has_low_rate_on_sunday(con):
    import datetime as dt
    start = dt.date(2025, 1, 6)
    for i in range(56):
        d = start + dt.timedelta(days=i)
        if d.weekday() < 5:          # только будни
            _day(con, d.isoformat())
    lifecycle.add_weekday_profile(con)
    rows = con.execute("""
        SELECT dayofweek(day) AS dow, max(dow_active_rate) AS r
        FROM feat_dow GROUP BY 1 ORDER BY 1
    """).fetchall()
    rates = dict(rows)
    assert 0 not in rates and 6 not in rates, "выходных в панели нет вовсе"
    assert all(r > 0.8 for r in rates.values()), "в будни канал активен почти всегда"


def test_days_since_same_dow(con):
    _day(con, "2025-01-06")   # понедельник
    _day(con, "2025-01-13")   # следующий понедельник
    _day(con, "2025-01-27")   # через две недели
    lifecycle.add_weekday_profile(con)
    got = {str(d): v for d, v in con.execute(
        "SELECT day, days_since_same_dow FROM feat_dow ORDER BY day").fetchall()}
    assert got["2025-01-13"] == 7
    assert got["2025-01-27"] == 14


def test_profile_does_not_leak_future_days(con):
    import datetime as dt
    start = dt.date(2025, 1, 6)
    for i in range(0, 56, 7):
        _day(con, (start + dt.timedelta(days=i)).isoformat())

    def rate_at(limit_rows: int) -> float:
        c = duckdb.connect(":memory:")
        c.execute("CREATE TABLE feat_ephist (ch BIGINT, day DATE, n_events BIGINT)")
        for i in range(0, limit_rows * 7, 7):
            c.execute("INSERT INTO feat_ephist VALUES (1, ?, 1)",
                      [(start + dt.timedelta(days=i)).isoformat()])
        lifecycle.add_weekday_profile(c)
        v = c.execute(
            "SELECT dow_active_rate FROM feat_dow WHERE day = DATE '2025-01-13'"
        ).fetchone()[0]
        c.close()
        return v

    assert rate_at(2) == pytest.approx(rate_at(8))
