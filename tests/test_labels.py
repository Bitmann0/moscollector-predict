import duckdb
import pytest

from mkl import labels


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE daily_channel (
        ch BIGINT, day DATE, obj VARCHAR, stype VARCHAR,
        n_events BIGINT, n_alarms BIGINT, n_bad BIGINT,
        n_fire BIGINT, n_intrusion BIGINT, picket DOUBLE,
        armed_eod INTEGER, last_arm_ts TIMESTAMP)"""); c.execute("""
        CREATE OR REPLACE VIEW _dc AS SELECT * FROM daily_channel""")
    c.execute("""CREATE TABLE episodes (ch BIGINT, obj VARCHAR, stype VARCHAR,
                 t_start TIMESTAMP, t_end TIMESTAMP, dur_s BIGINT,
                 n_events BIGINT, states VARCHAR, gap_before_s BIGINT,
                 is_group BOOLEAN)""")
    c.execute("""CREATE TABLE group_outages (obj VARCHAR, bucket BIGINT,
                 t_start TIMESTAMP, t_end TIMESTAMP, n_channels BIGINT)""")
    for d in range(1, 11):
        _dc(c, day=f"2025-01-{d:02d}")
    yield c
    c.close()



def _dc(c, ch=1, day="2025-01-01", obj="A", stype="Датчик дыма", n_events=5,
        n_alarms=0, n_bad=0, n_fire=0, n_intrusion=0, picket=10.0,
        armed_eod=None, last_arm_ts=None):
    """Строка панели с явными именами колонок.

    Позиционный INSERT ломался при каждом росте схемы — за время работы это
    случилось четырежды.
    """
    cols = dict(ch=ch, day=day, obj=obj, stype=stype, n_events=n_events,
                n_alarms=n_alarms, n_bad=n_bad, n_fire=n_fire,
                n_intrusion=n_intrusion, picket=picket, armed_eod=armed_eod,
                last_arm_ts=last_arm_ts)
    c.execute(f"INSERT INTO daily_channel ({', '.join(cols)}) VALUES "
              f"({', '.join('?' * len(cols))})", list(cols.values()))


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
        _dc(con, day=f"2025-01-{d:02d}")
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
        _dc(con, day=f"2025-01-{d:02d}")
    labels.build_sensor_failure(con, variant="L7", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 0
    labels.build_sensor_failure(con, variant="L4", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] > 0


def test_L7_catches_gap_in_regular_channel(con):
    """Канал отчитывался ежедневно месяц и пропал — это аномалия."""
    con.execute("DELETE FROM daily_channel")
    for d in list(range(1, 29)) + [31]:
        _dc(con, day=f"2025-01-{d:02d}")
    labels.build_sensor_failure(con, variant="L7", horizon_days=1)
    pos = [str(r[0]) for r in con.execute(
        "SELECT day FROM label_failure WHERE y=1").fetchall()]
    assert pos == ["2025-01-28"]


def test_L9c_censors_final_channel_report_without_return(con):
    for d in range(1, 9):
        _dc(con, ch=2, day=f"2025-01-{d:02d}")
    labels.build_sensor_failure(con, variant="L9", horizon_days=1,
                                table="label_link")
    assert con.execute(
        "SELECT y FROM label_link WHERE ch=2 AND day=DATE '2025-01-08'"
    ).fetchone() == (0,)
    labels.build_sensor_failure(con, variant="L9c", horizon_days=1,
                                table="label_link")
    assert con.execute(
        "SELECT count(*) FROM label_link WHERE ch=2 AND day=DATE '2025-01-08'"
    ).fetchone() == (0,)
    assert con.execute(
        "SELECT count(*) FROM label_link WHERE ch=2 AND day=DATE '2025-01-07'"
    ).fetchone() == (1,)


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
        _dc(con, ch=2, day=f"2025-01-{d:02d}", n_alarms=n_fire,
            n_fire=n_fire, picket=95.0)
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


def test_fire_label_excludes_unobservable_tail(con):
    """Последний день не имеет полного будущего окна и не является негативом."""
    labels.build_fire(con, horizon_days=1)
    days = {str(row[0]) for row in con.execute(
        "SELECT DISTINCT day FROM label_fire").fetchall()}
    assert "2025-01-09" in days
    assert "2025-01-10" not in days


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


def test_long_outage_is_caught_not_filtered_out(con):
    """Канал отчитывался 40 суток подряд, пропал на 60 и вернулся.

    Это самый крупный отказ, какой вообще бывает в данных, и прежде метка его
    не видела: окно активности считалось в день ВОЗВРАЩЕНИЯ и целиком
    накрывалось самим простоем. На реальных данных фильтр пропускал 0 из
    172 995 разрывов длиннее 30 суток.
    """
    import datetime as dt
    con.execute("DELETE FROM daily_channel")
    d0 = dt.date(2025, 1, 1)
    days = [d0 + dt.timedelta(days=i) for i in range(40)]
    days.append(d0 + dt.timedelta(days=99))
    for d in days:
        _dc(con, day=d.isoformat())
    labels.build_sensor_failure(con, variant="L4", horizon_days=1)
    pos = [str(r[0]) for r in
           con.execute("SELECT day FROM label_failure WHERE y=1").fetchall()]
    assert pos == ["2025-02-09"], "последние активные сутки: 1 янв + 39 суток"


def test_activity_is_measured_before_the_gap_not_after(con):
    """Прямая проверка отбора: два канала с одинаковым разрывом, но разной
    предысторией. Живший до пропажи — событие, впервые появившийся — нет."""
    import datetime as dt
    con.execute("DELETE FROM daily_channel")
    d0 = dt.date(2025, 1, 1)
    for i in range(30):                      # канал 1 активен весь месяц
        _dc(con, day=(d0 + dt.timedelta(days=i)).isoformat())
    for d in ("2025-01-29", "2025-02-20"):   # канал 2 отчитался дважды за всё время
        _dc(con, ch=2, day=d)
    _dc(con, day="2025-02-20")
    labels.build_sensor_failure(con, variant="L4", horizon_days=1)
    chs = sorted({r[0] for r in con.execute("SELECT ch FROM label_failure WHERE y=1").fetchall()})
    assert chs == [1]


# --- метка НСД по состоянию охраны -------------------------------------------

def _obj_day(c, obj, day, n_intrusion=0, armed=None, ch=1):
    _dc(c, ch=ch, day=day, obj=obj, stype="Охранный датчик",
        n_alarms=1 if n_intrusion else 0, n_intrusion=n_intrusion,
        armed_eod=armed,
        last_arm_ts=f"{day} 12:00:00" if armed is not None else None)


def test_intrusion_while_disarmed_is_not_unauthorised_access(con):
    """Тревога при снятой охране — проход персонала. Голова названа
    «несанкционированный доступ» и считала его позитивом наравне с нарушителем:
    по всей истории таких тревог 47.8%."""
    con.execute("DELETE FROM daily_channel")
    _obj_day(con, "A", "2025-01-01", armed=0)
    _obj_day(con, "A", "2025-01-02", n_intrusion=3, armed=0)
    _obj_day(con, "A", "2025-01-03", armed=0)
    labels.build_intrusion(con, horizon_days=1, armed_only=True)
    assert con.execute("SELECT sum(y) FROM label_intrusion").fetchone()[0] == 0


def test_intrusion_while_armed_is_a_positive(con):
    con.execute("DELETE FROM daily_channel")
    _obj_day(con, "A", "2025-01-01", armed=1)
    _obj_day(con, "A", "2025-01-02", n_intrusion=3, armed=1)
    _obj_day(con, "A", "2025-01-03", armed=1)
    labels.build_intrusion(con, horizon_days=1, armed_only=True)
    pos = [str(r[0]) for r in
           con.execute("SELECT day FROM label_intrusion WHERE y=1").fetchall()]
    assert pos == ["2025-01-01"]


def test_arming_state_carries_over_days_without_arming_events(con):
    """Объект стоит на охране неделями, а событие постановки одно."""
    con.execute("DELETE FROM daily_channel")
    _obj_day(con, "A", "2025-01-01", armed=1)
    _obj_day(con, "A", "2025-01-02")
    _obj_day(con, "A", "2025-01-03", n_intrusion=2)
    labels.build_intrusion(con, horizon_days=1, armed_only=True)
    assert con.execute("SELECT sum(y) FROM label_intrusion").fetchone()[0] == 1


def test_objects_without_arming_data_are_excluded_not_assumed_disarmed(con):
    """Охраны нет на 31 объекте из 78. NULL — это «неизвестно», и подставлять
    ноль нельзя: это превратило бы их в вечно снятые с охраны."""
    con.execute("DELETE FROM daily_channel")
    _obj_day(con, "B", "2025-01-01", n_intrusion=5)
    _obj_day(con, "B", "2025-01-02", n_intrusion=5)
    labels.build_intrusion(con, horizon_days=1, armed_only=True)
    objs = {r[0] for r in con.execute("SELECT obj FROM label_intrusion").fetchall()}
    assert "B" not in objs
