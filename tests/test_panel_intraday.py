"""Внутрисуточные признаки и метрики ISA-18.2 / EEMUA-191.

Час события несёт сигнал, которого нет больше нигде: тревоги концентрируются
в рабочие часы (42 тыс. в 11:00 против 5-8 тыс. ночью), то есть отражают
плановые работы, а не отказы оборудования. Это ближайший доступный заменитель
отсутствующих в выгрузке данных АРМ-Контроля.
"""
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
    """rows: (ts, val_raw, alarm)"""
    for i, (ts, val, alarm) in enumerate(rows):
        c.execute("INSERT INTO ev VALUES (?,1,?,?,?,?,NULL,'s','Датчик дыма',"
                  "'t','n','A',1.0)", [i, ts, ts[:10], alarm, val])


def test_night_fraction(con):
    _ins(con, [("2025-01-01 03:00:00", "Норма", False),
               ("2025-01-01 04:00:00", "Норма", False),
               ("2025-01-01 12:00:00", "Норма", False),
               ("2025-01-01 13:00:00", "Норма", False)])
    panel.build_daily_channel(con)
    row = con.execute(
        "SELECT night_frac, workhours_frac, n_active_hours FROM daily_channel"
    ).fetchone()
    assert row[0] == pytest.approx(0.5)
    assert row[1] == pytest.approx(0.5)
    assert row[2] == 4


def test_last_alarm_hour(con):
    _ins(con, [("2025-01-01 03:00:00", "Неисправен", True),
               ("2025-01-01 14:00:00", "Неисправен", True),
               ("2025-01-01 20:00:00", "Норма", False)])
    panel.build_daily_channel(con)
    assert con.execute(
        "SELECT last_alarm_hour FROM daily_channel").fetchone()[0] == 14


def test_night_alarm_fraction_separates_planned_from_real(con):
    """Ночная тревога значит не то же, что тревога в разгар рабочего дня."""
    _ins(con, [("2025-01-01 02:00:00", "Неисправен", True),
               ("2025-01-01 11:00:00", "Неисправен", True),
               ("2025-01-01 11:30:00", "Неисправен", True),
               ("2025-01-01 12:00:00", "Неисправен", True)])
    panel.build_daily_channel(con)
    assert con.execute(
        "SELECT night_alarm_frac FROM daily_channel").fetchone()[0] == pytest.approx(0.25)


def test_max_events_in_ten_minute_window(con):
    rows = [(f"2025-01-01 10:0{m}:00", "Норма", False) for m in range(0, 9)]
    rows += [("2025-01-01 14:00:00", "Норма", False)]
    _ins(con, rows)
    panel.build_daily_channel(con)
    assert con.execute("SELECT max_10min FROM daily_channel").fetchone()[0] == 9


def test_flood_bins_counted_by_eemua_threshold(con):
    """Флуд по EEMUA-191 — более 10 тревог за 10 минут."""
    rows = [(f"2025-01-01 10:0{m // 6}:{(m % 6) * 10:02d}", "Неисправен", True)
            for m in range(12)]
    _ins(con, rows)
    panel.build_daily_channel(con)
    row = con.execute(
        "SELECT max_alarm_10min, n_flood_bins FROM daily_channel").fetchone()
    assert row[0] == 12 and row[1] == 1


def test_chatter_index_high_for_rapid_repeats(con):
    """psi = sum(P_r / r): секундные интервалы дают индекс близко к единице."""
    _ins(con, [(f"2025-01-01 10:00:{s:02d}", "Неисправен", True)
               for s in range(0, 10)])
    panel.build_daily_channel(con)
    psi = con.execute("SELECT chatter_psi FROM daily_channel").fetchone()[0]
    assert psi == pytest.approx(1.0), "все интервалы по 1 с — psi равен единице"


def test_chatter_index_low_for_sparse_events(con):
    _ins(con, [("2025-01-01 10:00:00", "Норма", False),
               ("2025-01-01 11:00:00", "Норма", False),
               ("2025-01-01 12:00:00", "Норма", False)])
    panel.build_daily_channel(con)
    psi = con.execute("SELECT chatter_psi FROM daily_channel").fetchone()[0]
    assert psi < 0.001, "часовые интервалы дребезгом не являются"


def test_chatter_index_is_null_without_gaps(con):
    _ins(con, [("2025-01-01 10:00:00", "Норма", False)])
    panel.build_daily_channel(con)
    assert con.execute("SELECT chatter_psi FROM daily_channel").fetchone()[0] is None
