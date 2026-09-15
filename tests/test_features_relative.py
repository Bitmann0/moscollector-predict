import duckdb
import pytest

from mkl.features import lifecycle, relative


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE feat_base (
        ch BIGINT, day DATE, obj VARCHAR, stype VARCHAR, picket DOUBLE,
        n_events BIGINT, n_alarms BIGINT, n_bad BIGINT, n_fire BIGINT,
        n_transitions BIGINT, val_mean DOUBLE, val_max DOUBLE)""")
    yield c
    c.close()


def _row(c, ch, day, picket, n_alarms=0, n_bad=0, n_events=1, n_fire=0,
         n_transitions=0, val_mean=None, val_max=None):
    c.execute("INSERT INTO feat_base VALUES (?,?,'A','Датчик дыма',?,?,?,?,?,?,?,?)",
              [ch, day, picket, n_events, n_alarms, n_bad, n_fire,
               n_transitions, val_mean, val_max])


def test_peer_ratio_flags_outlier_channel(con):
    _row(con, 1, "2025-01-01", 10.0, n_alarms=100)
    _row(con, 2, "2025-01-01", 20.0, n_alarms=1)
    _row(con, 3, "2025-01-01", 30.0, n_alarms=1)
    relative.add_peer_features(con)
    got = dict(con.execute("SELECT ch, peer_ratio_alarms FROM feat_peer").fetchall())
    assert got[1] > 10
    assert got[2] == pytest.approx(1.0)


def test_peer_ratio_is_flat_when_whole_object_is_noisy(con):
    """Если шумит весь объект — это режим, а не деградация конкретного канала."""
    for ch in (1, 2, 3):
        _row(con, ch, "2025-01-01", ch * 10.0, n_alarms=100)
    relative.add_peer_features(con)
    got = dict(con.execute("SELECT ch, peer_ratio_alarms FROM feat_peer").fetchall())
    assert all(v == pytest.approx(1.0) for v in got.values())


def test_spatial_neighbour_picks_up_adjacent_segment(con):
    _row(con, 1, "2025-01-01", 10.0, n_bad=0)
    _row(con, 2, "2025-01-01", 12.0, n_bad=5)
    relative.add_peer_features(con)
    relative.add_spatial_features(con, radius_seg=1)
    got = dict(con.execute("SELECT ch, nbr_bad FROM feat_spatial").fetchall())
    assert got[1] == 5


def test_spatial_excludes_own_contribution(con):
    _row(con, 1, "2025-01-01", 10.0, n_bad=7)
    relative.add_peer_features(con)
    relative.add_spatial_features(con, radius_seg=1)
    assert con.execute("SELECT nbr_bad FROM feat_spatial").fetchone()[0] == 0


def test_spatial_ignores_distant_segment(con):
    _row(con, 1, "2025-01-01", 10.0, n_bad=0)
    _row(con, 2, "2025-01-01", 900.0, n_bad=5)
    relative.add_peer_features(con)
    relative.add_spatial_features(con, radius_seg=1)
    got = dict(con.execute("SELECT ch, nbr_bad FROM feat_spatial").fetchall())
    assert got[1] == 0


def test_age_days_counts_from_first_seen(con):
    _row(con, 1, "2025-01-01", 10.0)
    _row(con, 1, "2025-01-11", 10.0)
    relative.add_peer_features(con)
    relative.add_spatial_features(con)
    lifecycle.add_lifecycle_features(con)
    got = {str(d): v for d, v in con.execute(
        "SELECT day, age_days FROM feat_full ORDER BY day").fetchall()}
    assert got["2025-01-01"] == 0
    assert got["2025-01-11"] == 10


def test_duty_cycles_accumulate_within_30_days(con):
    _row(con, 1, "2025-01-01", 10.0, n_transitions=4)
    _row(con, 1, "2025-01-10", 10.0, n_transitions=6)
    _row(con, 1, "2025-03-01", 10.0, n_transitions=1)
    relative.add_peer_features(con)
    relative.add_spatial_features(con)
    lifecycle.add_lifecycle_features(con)
    got = {str(d): v for d, v in con.execute(
        "SELECT day, n_duty_cycles_w30 FROM feat_full ORDER BY day").fetchall()}
    assert got["2025-01-10"] == 10
    assert got["2025-03-01"] == 1, "окно 30 суток не должно тянуть январь"


def test_prior_failures_do_not_count_the_future(con):
    _row(con, 1, "2025-01-01", 10.0, n_bad=0)
    _row(con, 1, "2025-01-02", 10.0, n_bad=3)
    relative.add_peer_features(con)
    relative.add_spatial_features(con)
    lifecycle.add_lifecycle_features(con)
    got = {str(d): v for d, v in con.execute(
        "SELECT day, n_prior_failures FROM feat_full ORDER BY day").fetchall()}
    assert got["2025-01-01"] == 0
