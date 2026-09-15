import duckdb
import pytest

from mkl import states


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
    for i, (ch, ts, val, obj) in enumerate(rows):
        c.execute(
            "INSERT INTO ev VALUES (?,?,?,?,false,?,NULL,'s','Датчик дыма','t','n',?,1.0)",
            [i, ch, ts, ts[:10], val, obj],
        )


def test_consecutive_bad_states_merge_into_one_episode(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Норма", "A"),
        (1, "2025-01-01 11:00:00", "Неопределен", "A"),
        (1, "2025-01-01 12:00:00", "Обесточен", "A"),
        (1, "2025-01-01 14:00:00", "Норма", "A"),
    ])
    states.build_episodes(con)
    ep = con.execute("SELECT ch, dur_s, n_events FROM episodes").fetchall()
    assert ep == [(1, 3600, 2)]


def test_episode_broken_by_normal_state(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Неисправен", "A"),
        (1, "2025-01-01 10:30:00", "Норма", "A"),
        (1, "2025-01-01 11:00:00", "Неисправен", "A"),
    ])
    states.build_episodes(con)
    assert con.execute("SELECT count(*) FROM episodes").fetchone()[0] == 2


def test_group_outage_flags_simultaneous_channels(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Неопределен", "A"),
        (2, "2025-01-01 10:01:00", "Неопределен", "A"),
        (3, "2025-01-01 10:02:00", "Неопределен", "A"),
        (4, "2025-01-01 10:03:00", "Неопределен", "A"),
        (9, "2025-02-01 10:00:00", "Неопределен", "B"),
    ])
    states.build_all(con)
    grouped = con.execute("SELECT ch FROM episodes WHERE is_group ORDER BY ch").fetchall()
    assert grouped == [(1,), (2,), (3,), (4,)]
    solo = con.execute("SELECT ch FROM episodes WHERE NOT is_group").fetchall()
    assert solo == [(9,)]


def test_numeric_channels_do_not_produce_episodes(con):
    con.execute(
        "INSERT INTO ev VALUES (1,1,'2025-01-01 10:00:00','2025-01-01',false,"
        "'0.02',0.02,'s','Газовый датчик','t','n','A',1.0)"
    )
    states.build_episodes(con)
    assert con.execute("SELECT count(*) FROM episodes").fetchone()[0] == 0
