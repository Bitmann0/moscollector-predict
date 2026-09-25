"""Длительность состояния, охрана и неиспользованные состояния.

В панели не было ни одного признака времени: только счётчики событий. Между тем
деградация и износ по определению есть удлинение времени в ненормальном
состоянии, а тревога проникновения при снятой охране — это проход персонала,
а не нарушитель.
"""
import duckdb
import pytest

from conftest import EV_SCHEMA, insert_event
from mkl import panel


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(EV_SCHEMA)
    yield c
    c.close()


def _ins(c, rows):
    """rows: (ts, val_raw, alarm)"""
    for i, (ts, val, alarm) in enumerate(rows):
        insert_event(c, i, 1, ts, val, alarm=alarm)


def _one(c, col):
    panel.build_daily_channel(c)
    return c.execute(f"SELECT {col} FROM daily_channel").fetchone()[0]


def test_time_in_alarm_counts_until_the_next_event(con):
    _ins(con, [("2025-01-01 10:00:00", "Неисправен", True),
               ("2025-01-01 12:00:00", "Норма", False),
               ("2025-01-01 13:00:00", "Норма", False)])
    assert _one(con, "time_in_alarm_s") == 7200


def test_open_alarm_is_truncated_at_end_of_day(con):
    """Тревога, не закрытая до полуночи, считается до конца суток.

    Ждать закрытия нельзя: признак должен быть готов в конце суток t, а метка
    живёт в t+1. Иначе это взгляд в будущее.
    """
    _ins(con, [("2025-01-01 23:00:00", "Неисправен", True)])
    assert _one(con, "time_in_alarm_s") == 3600


def test_standing_alarm_counted_by_four_hour_threshold(con):
    _ins(con, [("2025-01-01 01:00:00", "Неисправен", True),
               ("2025-01-01 06:00:00", "Норма", False),
               ("2025-01-01 07:00:00", "Неисправен", True),
               ("2025-01-01 08:00:00", "Норма", False)])
    panel.build_daily_channel(con)
    row = con.execute(
        "SELECT n_standing_4h, max_hold_alarm_s FROM daily_channel").fetchone()
    assert row == (1, 18000)


def test_time_in_bad_is_separate_from_time_in_alarm(con):
    """Плохое состояние и тревожный флаг — разные вещи и расходятся в данных."""
    _ins(con, [("2025-01-01 10:00:00", "Обесточен", False),
               ("2025-01-01 11:00:00", "Норма", False)])
    panel.build_daily_channel(con)
    row = con.execute(
        "SELECT time_in_alarm_s, time_in_bad_s FROM daily_channel").fetchone()
    assert row == (0, 3600)


def test_armed_state_at_end_of_day_takes_the_last_event(con):
    _ins(con, [("2025-01-01 07:00:00", "Снято с охраны", False),
               ("2025-01-01 17:00:00", "На охране", False)])
    panel.build_daily_channel(con)
    row = con.execute(
        "SELECT n_arm, n_disarm, armed_eod FROM daily_channel").fetchone()
    assert row == (1, 1, 1)


def test_armed_state_is_null_without_arming_events(con):
    """Охрана есть только на 47 объектах из 78. Отсутствие события — не
    «снято с охраны», а неизвестно, и путать их нельзя."""
    _ins(con, [("2025-01-01 10:00:00", "Норма", False)])
    assert _one(con, "armed_eod") is None


def test_chatter_index_over_alarms_ignores_polling(con):
    """Общий psi на числовом канале меряет период опроса, а не дребезг:
    газ и температура дают 168 млн событий на ~500 каналов.
    """
    rows = [(f"2025-01-01 10:00:{s:02d}", "0.1", False) for s in range(0, 10)]
    rows += [("2025-01-01 14:00:00", "Неисправен", True),
             ("2025-01-01 15:00:00", "Неисправен", True)]
    _ins(con, rows)
    panel.build_daily_channel(con)
    row = con.execute(
        "SELECT chatter_psi, chatter_psi_alarm FROM daily_channel").fetchone()
    assert row[0] > 0.8, "общий индекс видит секундный опрос как дребезг"
    assert row[1] < 0.001, "по тревогам дребезга нет — они раз в час"


def test_unused_states_are_counted(con):
    _ins(con, [("2025-01-01 10:00:00", "Много неисправных устройств", True),
               ("2025-01-01 11:00:00", "Питание от батарей", False),
               ("2025-01-01 12:00:00", "Разговор", False),
               ("2025-01-01 13:00:00", "Вызов", False)])
    panel.build_daily_channel(con)
    row = con.execute("SELECT n_many_bad, n_battery_power, n_talk, n_call "
                      "FROM daily_channel").fetchone()
    assert row == (1, 1, 1, 1)
