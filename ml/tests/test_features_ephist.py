"""История завершённых отказных эпизодов канала.

Признак строго прошлый: учитываются только эпизоды, закончившиеся не позже
текущих суток, тогда как метка спрашивает про эпизоды строго в будущем.
"""
import duckdb
import pytest

from mkl.features import lifecycle


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE feat_full (
        ch BIGINT, day DATE, age_days BIGINT, n_prior_failures BIGINT)""")
    c.execute("""CREATE TABLE episodes (ch BIGINT, t_start TIMESTAMP,
        t_end TIMESTAMP, dur_s BIGINT, is_group BOOLEAN)""")
    for d in range(1, 21):
        c.execute("INSERT INTO feat_full VALUES (1, ?, ?, 0)",
                  [f"2025-01-{d:02d}", 100 + d])
    yield c
    c.close()


def _ep(c, start, end, dur=7200, is_group=False, ch=1):
    c.execute("INSERT INTO episodes VALUES (?,?,?,?,?)", [ch, start, end, dur, is_group])


def test_prior_episodes_accumulate(con):
    _ep(con, "2025-01-05 01:00:00", "2025-01-05 03:00:00")
    _ep(con, "2025-01-10 01:00:00", "2025-01-10 03:00:00")
    lifecycle.add_episode_history(con)
    got = {str(d): v for d, v in con.execute(
        "SELECT day, n_prior_episodes FROM feat_ephist ORDER BY day").fetchall()}
    assert got["2025-01-04"] == 0
    assert got["2025-01-05"] == 1
    assert got["2025-01-09"] == 1
    assert got["2025-01-20"] == 2


def test_episode_ending_in_the_future_is_not_counted(con):
    """Эпизод, который ещё не закончился, на сегодня неизвестен."""
    _ep(con, "2025-01-05 01:00:00", "2025-01-18 03:00:00", dur=13 * 86400)
    lifecycle.add_episode_history(con)
    got = {str(d): v for d, v in con.execute(
        "SELECT day, n_prior_episodes FROM feat_ephist ORDER BY day").fetchall()}
    assert got["2025-01-10"] == 0
    assert got["2025-01-18"] == 1


def test_group_episodes_are_excluded(con):
    _ep(con, "2025-01-05 01:00:00", "2025-01-05 03:00:00", is_group=True)
    lifecycle.add_episode_history(con)
    assert con.execute(
        "SELECT max(n_prior_episodes) FROM feat_ephist"
    ).fetchone()[0] == 0


def test_short_episodes_are_excluded(con):
    _ep(con, "2025-01-05 01:00:00", "2025-01-05 01:00:30", dur=30)
    lifecycle.add_episode_history(con)
    assert con.execute(
        "SELECT max(n_prior_episodes) FROM feat_ephist"
    ).fetchone()[0] == 0


def test_days_since_prior_episode(con):
    _ep(con, "2025-01-05 01:00:00", "2025-01-05 03:00:00")
    lifecycle.add_episode_history(con)
    got = {str(d): v for d, v in con.execute(
        "SELECT day, days_since_prior_episode FROM feat_ephist ORDER BY day").fetchall()}
    assert got["2025-01-05"] == 0
    assert got["2025-01-12"] == 7


def test_episodes_per_year_is_normalised_by_age(con):
    _ep(con, "2025-01-05 01:00:00", "2025-01-05 03:00:00")
    lifecycle.add_episode_history(con)
    row = con.execute(
        "SELECT age_days, episodes_per_year FROM feat_ephist WHERE day = DATE '2025-01-20'"
    ).fetchone()
    assert row[1] == pytest.approx(365.0 / row[0])
