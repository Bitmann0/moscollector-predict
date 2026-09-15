import duckdb
import pytest

from mkl import panel


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE ev (event_id BIGINT, ch BIGINT, ts TIMESTAMP, day DATE,
                         alarm BOOLEAN, val_raw VARCHAR, val_num DOUBLE,
                         sys VARCHAR, stype VARCHAR, tag VARCHAR, sname VARCHAR,
                         obj VARCHAR, picket DOUBLE)
    """)
    yield c
    c.close()


def _ins(c, rows):
    for i, (ch, ts, val, alarm, num) in enumerate(rows):
        c.execute(
            "INSERT INTO ev VALUES (?,?,?,?,?,?,?,'s','Датчик дыма','t','n','A',1.0)",
            [i, ch, ts, ts[:10], alarm, val, num],
        )


def test_counts_events_and_alarms_per_day(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Норма", False, None),
        (1, "2025-01-01 11:00:00", "Неисправен", True, None),
        (1, "2025-01-02 10:00:00", "Норма", False, None),
    ])
    panel.build_daily_channel(con)
    got = con.execute(
        "SELECT day, n_events, n_alarms, n_bad FROM daily_channel ORDER BY day"
    ).fetchall()
    assert got[0][1:] == (2, 1, 1)
    assert got[1][1:] == (1, 0, 0)


def test_chatter_counts_three_events_within_one_minute(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Неисправен", True, None),
        (1, "2025-01-01 10:00:20", "Норма", False, None),
        (1, "2025-01-01 10:00:40", "Неисправен", True, None),
    ])
    panel.build_daily_channel(con)
    assert con.execute("SELECT n_chatter_1min FROM daily_channel").fetchone()[0] >= 1


def test_chatter_ignores_slow_sequences(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Неисправен", True, None),
        (1, "2025-01-01 10:30:00", "Норма", False, None),
        (1, "2025-01-01 11:00:00", "Неисправен", True, None),
    ])
    panel.build_daily_channel(con)
    assert con.execute("SELECT n_chatter_1min FROM daily_channel").fetchone()[0] == 0


def test_numeric_aggregates(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "0.10", False, 0.10),
        (1, "2025-01-01 11:00:00", "0.30", False, 0.30),
    ])
    panel.build_daily_channel(con)
    row = con.execute("SELECT val_min, val_max, val_mean FROM daily_channel").fetchone()
    assert row[0] == pytest.approx(0.10)
    assert row[1] == pytest.approx(0.30)
    assert row[2] == pytest.approx(0.20)


def test_max_gap_is_computed_within_day(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Норма", False, None),
        (1, "2025-01-01 12:00:00", "Норма", False, None),
    ])
    panel.build_daily_channel(con)
    assert con.execute("SELECT max_gap_s FROM daily_channel").fetchone()[0] == 7200
