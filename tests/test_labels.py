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
                 n_events BIGINT, states VARCHAR, gap_before_s BIGINT,
                 is_group BOOLEAN)""")
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
             stype="Датчик дыма", gap_before_s=600):
    c.execute("INSERT INTO episodes VALUES (?, 'A', ?, ?, ?, ?, 3, ?, ?, ?)",
              [ch, stype, start, end, dur, states, gap_before_s, is_group])


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


def test_L6_rejects_dormant_channel(con):
    """Канал молчал полгода — это не отказ, а спящий или списанный канал."""
    _episode(con, "2025-01-05 03:00:00", "2025-01-05 09:00:00", 21600,
             gap_before_s=200 * 86400)
    labels.build_sensor_failure(con, variant="L6", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 0
    labels.build_sensor_failure(con, variant="L3", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 1


def test_L6_rejects_decommissioning_length_episode(con):
    """Эпизод длиной 200 суток — это вывод из эксплуатации, а не отказ."""
    _episode(con, "2025-01-05 03:00:00", "2025-07-24 03:00:00", 200 * 86400)
    labels.build_sensor_failure(con, variant="L6", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 0


def test_L6_accepts_live_channel_with_bounded_episode(con):
    _episode(con, "2025-01-05 03:00:00", "2025-01-05 09:00:00", 21600,
             gap_before_s=600)
    labels.build_sensor_failure(con, variant="L6", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 1


def test_L4_silence_produces_positives(con):
    """Канал пропустил сутки после месяца регулярной работы — это молчание."""
    con.execute("DELETE FROM daily_channel")
    for d in list(range(1, 16)) + [18, 19, 20]:
        con.execute(
            "INSERT INTO daily_channel VALUES (1, ?, 'A', 'Датчик дыма', 5, 0, 0, 0, 0, 10.0)",
            [f"2025-01-{d:02d}"],
        )
    labels.build_sensor_failure(con, variant="L4", horizon_days=1)
    total = con.execute("SELECT sum(y) FROM label_failure").fetchone()[0]
    assert total > 0, "разрыв 15 -> 18 января обязан дать положительную метку"
    pos = [str(r[0]) for r in con.execute(
        "SELECT day FROM label_failure WHERE y=1 ORDER BY day").fetchall()]
    assert "2025-01-15" in pos


def test_L7_ignores_channel_that_reports_rarely(con):
    """Канал, штатно отчитывающийся раз в трое суток, пропуском не отказывает."""
    con.execute("DELETE FROM daily_channel")
    for d in range(1, 31, 3):
        con.execute(
            "INSERT INTO daily_channel VALUES (1, ?, 'A', 'Датчик дыма', 5, 0, 0, 0, 0, 10.0)",
            [f"2025-01-{d:02d}"],
        )
    labels.build_sensor_failure(con, variant="L7", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 0
    labels.build_sensor_failure(con, variant="L4", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] > 0


def test_L7_catches_gap_in_regular_channel(con):
    """Канал отчитывался ежедневно месяц и пропал — это аномалия."""
    con.execute("DELETE FROM daily_channel")
    for d in list(range(1, 29)) + [31]:
        con.execute(
            "INSERT INTO daily_channel VALUES (1, ?, 'A', 'Датчик дыма', 5, 0, 0, 0, 0, 10.0)",
            [f"2025-01-{d:02d}"],
        )
    labels.build_sensor_failure(con, variant="L7", horizon_days=1)
    pos = [str(r[0]) for r in con.execute(
        "SELECT day FROM label_failure WHERE y=1").fetchall()]
    assert pos == ["2025-01-28"]


def test_horizon_widens_positive_window(con):
    """Данные в фикстуре кончаются 01-10, поэтому при горизонте 7 суток метку
    можно ставить только до 01-03: у 01-04 и дальше окно уже выходит за край."""
    _episode(con, "2025-01-08 03:00:00", "2025-01-08 09:00:00", 21600)
    labels.build_sensor_failure(con, variant="L2", horizon_days=7)
    pos = sorted(str(r[0]) for r in
                 con.execute("SELECT day FROM label_failure WHERE y=1").fetchall())
    assert pos == [f"2025-01-{d:02d}" for d in range(1, 4)]


def test_censored_tail_is_dropped_not_marked_negative(con):
    """Для последних суток окно (day, day+H] выходит за конец данных, и событие
    там невидимо. Если оставить такие сутки в выборке, они станут отрицательными
    без всякого основания: на реальных данных 2026-06-30 давала 2 716 строк и
    ровно ноль положительных при базовой ставке 0.26."""
    labels.build_sensor_failure(con, variant="L2", horizon_days=1)
    days = [str(r[0]) for r in
            con.execute("SELECT day FROM label_failure ORDER BY day").fetchall()]
    assert "2025-01-10" not in days, "последние сутки цензурированы"
    assert days[-1] == "2025-01-09"


def test_censored_tail_grows_with_horizon(con):
    labels.build_sensor_failure(con, variant="L2", horizon_days=3)
    last = con.execute("SELECT max(day) FROM label_failure").fetchone()[0]
    assert str(last) == "2025-01-07"


def test_horizon_window_does_not_reach_into_excluded_period(con, monkeypatch):
    """Сутки миграции СМВУ отбрасываются при обучении, но их тень через горизонт
    дотягивалась до соседних: на 2021-03-31 метка давала 346 положительных,
    указывающих на артефакты внутри исключённого периода."""
    import datetime as dt
    monkeypatch.setattr(labels, "EXCLUDED_PERIODS",
                        [(dt.date(2025, 1, 6), dt.date(2025, 1, 8))])
    _episode(con, "2025-01-07 03:00:00", "2025-01-07 09:00:00", 21600)
    labels.build_sensor_failure(con, variant="L2", horizon_days=1)
    days = [str(r[0]) for r in
            con.execute("SELECT day FROM label_failure ORDER BY day").fetchall()]
    assert "2025-01-05" not in days, "окно этих суток смотрит внутрь исключённого периода"
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 0


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


def test_flood_label_covers_only_objects_with_pumps(con):
    """Там, где насосов нет, подтопление ничем не измеряется."""
    con.execute("DELETE FROM daily_channel")
    con.execute("ALTER TABLE daily_channel ADD COLUMN IF NOT EXISTS n_flood BIGINT DEFAULT 0")
    for d in range(1, 11):
        # объект A с насосом, объект B без
        con.execute(
            "INSERT INTO daily_channel (ch, day, obj, stype, n_events, n_alarms,"
            " n_bad, n_fire, n_intrusion, picket, n_flood) "
            "VALUES (1, ?, 'A', 'Состояние насоса', 5, 0, 0, 0, 0, 10.0, ?)",
            [f"2025-01-{d:02d}", 1 if d == 5 else 0])
        con.execute(
            "INSERT INTO daily_channel (ch, day, obj, stype, n_events, n_alarms,"
            " n_bad, n_fire, n_intrusion, picket, n_flood) "
            "VALUES (2, ?, 'B', 'Датчик дыма', 5, 0, 0, 0, 0, 10.0, 0)",
            [f"2025-01-{d:02d}"])
    labels.build_flood(con, horizon_days=1)
    objs = {r[0] for r in con.execute("SELECT DISTINCT obj FROM label_flood").fetchall()}
    assert objs == {"A"}, "объект без насосов в популяцию попадать не должен"
    pos = [str(r[0]) for r in
           con.execute("SELECT day FROM label_flood WHERE y = 1").fetchall()]
    assert pos == ["2025-01-04"]
