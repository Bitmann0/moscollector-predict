import duckdb
import pytest

from mkl import labels


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE daily_channel (
        ch BIGINT, day DATE, obj VARCHAR, stype VARCHAR,
        n_events BIGINT, n_alarms BIGINT, n_bad BIGINT,
        n_fire BIGINT, n_intrusion BIGINT, picket DOUBLE)""")
    c.execute("""CREATE TABLE episodes (ch BIGINT, obj VARCHAR, stype VARCHAR,
                 t_start TIMESTAMP, t_end TIMESTAMP, dur_s BIGINT,
                 n_events BIGINT, states VARCHAR, is_group BOOLEAN)""")
    c.execute("""CREATE TABLE group_outages (obj VARCHAR, bucket BIGINT,
                 t_start TIMESTAMP, t_end TIMESTAMP, n_channels BIGINT)""")
    for d in range(1, 11):
        c.execute(
            "INSERT INTO daily_channel VALUES (1, ?, 'A', 'Датчик дыма', 5, 0, 0, 0, 0, 10.0)",
            [f"2025-01-{d:02d}"],
        )
    yield c
    c.close()


def _episode(c, start, end, dur, is_group=False, states="Обесточен", ch=1,
             stype="Датчик дыма"):
    c.execute("INSERT INTO episodes VALUES (?, 'A', ?, ?, ?, ?, 3, ?, ?)",
              [ch, stype, start, end, dur, states, is_group])


def test_label_marks_day_before_failure(con):
    _episode(con, "2025-01-05 03:00:00", "2025-01-05 09:00:00", 21600)
    labels.build_sensor_failure(con, variant="L2", horizon_days=1)
    pos = [str(r[0]) for r in
           con.execute("SELECT day FROM label_failure WHERE y=1 ORDER BY day").fetchall()]
    assert pos == ["2025-01-04"]


def test_failure_day_itself_is_not_labelled(con):
    """Метка строго будущая: событие в сами сутки не делает их положительными."""
    _episode(con, "2025-01-05 03:00:00", "2025-01-05 09:00:00", 21600)
    labels.build_sensor_failure(con, variant="L2", horizon_days=1)
    y = con.execute(
        "SELECT y FROM label_failure WHERE day = DATE '2025-01-05'"
    ).fetchone()[0]
    assert y == 0


def test_short_episode_is_not_a_failure(con):
    _episode(con, "2025-01-05 03:00:00", "2025-01-05 03:00:30", 30, states="Неисправен")
    labels.build_sensor_failure(con, variant="L2", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 0


def test_L3_excludes_group_episodes(con):
    _episode(con, "2025-01-05 03:00:00", "2025-01-05 09:00:00", 21600, is_group=True)
    labels.build_sensor_failure(con, variant="L3", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 0
    labels.build_sensor_failure(con, variant="L2", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 1


def test_horizon_widens_positive_window(con):
    _episode(con, "2025-01-08 03:00:00", "2025-01-08 09:00:00", 21600)
    labels.build_sensor_failure(con, variant="L2", horizon_days=7)
    pos = sorted(str(r[0]) for r in
                 con.execute("SELECT day FROM label_failure WHERE y=1").fetchall())
    assert pos == [f"2025-01-{d:02d}" for d in range(1, 8)]


def test_group_outage_label_uses_object_entity(con):
    con.execute(
        "INSERT INTO group_outages VALUES ('A', 1, '2025-01-05 03:00:00', "
        "'2025-01-05 03:04:00', 12)"
    )
    labels.build_group_outage(con, horizon_days=1)
    cols = [r[0] for r in con.execute("DESCRIBE label_group_outage").fetchall()]
    assert cols[:2] == ["obj", "day"]
    assert con.execute("SELECT sum(y) FROM label_group_outage").fetchone()[0] == 1


def test_fire_label_is_per_segment(con):
    for d in range(1, 11):
        n_fire = 1 if d == 5 else 0
        con.execute(
            "INSERT INTO daily_channel VALUES (2, ?, 'A', 'Датчик дыма', 5, ?, 0, ?, 0, 95.0)",
            [f"2025-01-{d:02d}", n_fire, n_fire],
        )
    labels.build_fire(con, horizon_days=1)
    rows = dict(con.execute(
        "SELECT seg, sum(y) FROM label_fire GROUP BY seg ORDER BY seg"
    ).fetchall())
    assert rows[9] == 1, "сутки перед задымлением на участке 9 должны быть положительными"
    assert rows[1] == 0, "участок 1 задымления не видел"
    pos_day = con.execute(
        "SELECT day FROM label_fire WHERE seg = 9 AND y = 1"
    ).fetchone()[0]
    assert str(pos_day) == "2025-01-04"


def test_wear_label_only_covers_equipment_channels(con):
    _episode(con, "2025-01-05 03:00:00", "2025-01-05 09:00:00", 21600,
             stype="Состояние насоса")
    labels.build_wear(con, horizon_days=7)
    assert con.execute("SELECT count(*) FROM label_wear").fetchone()[0] == 0, \
        "канал не помечен как оборудование в daily_channel — строк быть не должно"
