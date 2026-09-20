import duckdb
import pytest

from conftest import EV_SCHEMA, insert_event
from mkl import states


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(EV_SCHEMA)
    yield c
    c.close()


def _ins(c, rows):
    for i, (ch, ts, val, obj) in enumerate(rows):
        insert_event(c, i, ch, ts, val, obj=obj)


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


def _outage(c, ch, start_min, obj="A", hours=3):
    """Устойчивый отказ канала: ушёл в плохое состояние и не вернулся hours часов."""
    base = 10 * 60 + start_min
    for k in range(hours + 1):
        insert_event(c, ch * 1000 + k, ch,
                     f"2025-01-01 {(base + k * 60) // 60:02d}:{(base + k * 60) % 60:02d}:00",
                     "Неопределен", obj=obj)


def test_group_outage_flags_simultaneous_channels(con):
    for ch in (1, 2, 3, 4):
        _outage(con, ch, ch)
    _outage(con, 9, 0, obj="B")
    states.build_all(con)
    grouped = con.execute("SELECT DISTINCT ch FROM episodes WHERE is_group ORDER BY ch").fetchall()
    assert grouped == [(1,), (2,), (3,), (4,)]
    solo = con.execute("SELECT DISTINCT ch FROM episodes WHERE NOT is_group").fetchall()
    assert solo == [(9,)]


def test_chatter_does_not_make_a_group_outage(con):
    """Четыре канала мигнули в одном пятиминутном окне и тут же вернулись.

    Это фон, а не массовый отказ. Пока групповые окна строились по всем эпизодам
    подряд, 92.7% из них не содержали ни одного эпизода длиннее часа, и метка
    «массовый отказ объекта» на деле описывала кластеры дребезга.
    """
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Неопределен", "A"),
        (2, "2025-01-01 10:01:00", "Неопределен", "A"),
        (3, "2025-01-01 10:02:00", "Неопределен", "A"),
        (4, "2025-01-01 10:03:00", "Неопределен", "A"),
    ])
    states.build_all(con)
    assert con.execute("SELECT count(*) FROM group_outages").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM episodes WHERE is_group").fetchone()[0] == 0


def test_sustained_failure_is_not_excluded_from_solo_label(con):
    """Обратная сторона той же правки: пока дребезг создавал групповые окна,
    в них попадали и настоящие отказы — фильтр NOT is_group выбрасывал из метки
    одиночного отказа 78% эпизодов длиннее часа (15 747 из 71 725).
    """
    _outage(con, 1, 0)
    _ins(con, [(2, "2025-01-01 10:01:00", "Неопределен", "A"),
               (3, "2025-01-01 10:02:00", "Неопределен", "A"),
               (4, "2025-01-01 10:03:00", "Неопределен", "A")])
    states.build_all(con)
    assert con.execute(
        "SELECT count(*) FROM episodes WHERE ch = 1 AND NOT is_group").fetchone()[0] == 1


def test_gap_before_measures_dormancy(con):
    """Канал, молчавший полгода, не должен выглядеть как внезапный отказ."""
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Норма", "A"),
        (1, "2025-07-01 10:00:00", "Обесточен", "A"),
        (2, "2025-01-01 10:00:00", "Норма", "A"),
        (2, "2025-01-01 11:00:00", "Обесточен", "A"),
    ])
    states.build_episodes(con)
    got = dict(con.execute("SELECT ch, gap_before_s FROM episodes").fetchall())
    assert got[1] > 180 * 86400
    assert got[2] == 3600


def test_numeric_channels_do_not_produce_episodes(con):
    insert_event(con, 1, 1, "2025-01-01 10:00:00", "0.02", val_num=0.02,
                 stype="Газовый датчик")
    states.build_episodes(con)
    assert con.execute("SELECT count(*) FROM episodes").fetchone()[0] == 0
