"""Контекст комплекса для объектных голов.

78 объектов входят в 16 комплексов. Общая авария питания или обрыв магистрали
видны на комплексе целиком, и для отдельного объекта состояние соседей —
внешний предиктор. При привязке по префиксу тега этих групп просто не
существовало.
"""
import duckdb
import pytest

from mkl.features import compute


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE feat_object (
        obj VARCHAR, day DATE, obj_parent VARCHAR, is_guard_object INTEGER,
        n_channels BIGINT, n_events BIGINT, n_alarms BIGINT, n_bad BIGINT,
        n_bad_w7 BIGINT, n_intrusion BIGINT, n_flood BIGINT, n_fire BIGINT)""")
    yield c
    c.close()


def _row(c, obj, par, day="2025-01-01", n_bad=0, n_alarms=0, n_channels=10,
         n_intrusion=0):
    c.execute("INSERT INTO feat_object VALUES (?,?,?,1,?,100,?,?,?,?,0,0)",
              [obj, day, par, n_channels, n_alarms, n_bad, n_bad, n_intrusion])


def test_sibling_sums_exclude_the_object_itself(con):
    _row(con, "A", "P", n_bad=5)
    _row(con, "B", "P", n_bad=3)
    _row(con, "C", "P", n_bad=2)
    compute.add_complex_context(con)
    got = dict(con.execute("SELECT obj, par_n_bad FROM feat_object").fetchall())
    assert got["A"] == 5, "сумма соседей без самого объекта"
    assert got["B"] == 7
    assert got["C"] == 8


def test_sibling_count_excludes_self(con):
    _row(con, "A", "P")
    _row(con, "B", "P")
    _row(con, "C", "P")
    compute.add_complex_context(con)
    v = con.execute("SELECT DISTINCT par_n_siblings FROM feat_object").fetchone()[0]
    assert v == 2


def test_complexes_do_not_leak_into_each_other(con):
    _row(con, "A", "P", n_bad=0)
    _row(con, "B", "P", n_bad=0)
    _row(con, "X", "Q", n_bad=99)
    compute.add_complex_context(con)
    got = dict(con.execute("SELECT obj, par_n_bad FROM feat_object").fetchall())
    assert got["A"] == 0 and got["B"] == 0


def test_fraction_of_bad_siblings(con):
    _row(con, "A", "P", n_bad=0)
    _row(con, "B", "P", n_bad=1)
    _row(con, "C", "P", n_bad=1)
    _row(con, "D", "P", n_bad=0)
    compute.add_complex_context(con)
    got = dict(con.execute(
        "SELECT obj, par_frac_objects_bad FROM feat_object").fetchall())
    assert got["A"] == pytest.approx(2 / 3), "двое из трёх соседей в отказе"
    assert got["B"] == pytest.approx(1 / 3)


def test_share_of_complex_is_relative_to_whole(con):
    _row(con, "A", "P", n_bad=3)
    _row(con, "B", "P", n_bad=1)
    compute.add_complex_context(con)
    got = dict(con.execute("SELECT obj, par_share_bad FROM feat_object").fetchall())
    assert got["A"] == pytest.approx(0.75)


def test_lone_object_in_complex_gives_null_not_error(con):
    """Комплекс из одного объекта: делить не на что, но падать нельзя."""
    _row(con, "A", "P", n_bad=4)
    compute.add_complex_context(con)
    row = con.execute(
        "SELECT par_n_siblings, par_n_bad, par_frac_objects_bad FROM feat_object"
    ).fetchone()
    assert row[0] == 0 and row[1] == 0 and row[2] is None


def test_context_is_same_day_only(con):
    _row(con, "A", "P", "2025-01-01", n_bad=0)
    _row(con, "B", "P", "2025-01-01", n_bad=0)
    _row(con, "B", "P", "2025-01-02", n_bad=50)
    compute.add_complex_context(con)
    v = con.execute("SELECT par_n_bad FROM feat_object "
                    "WHERE obj = 'A' AND day = DATE '2025-01-01'").fetchone()[0]
    assert v == 0, "контекст не должен тянуть будущие сутки"


def test_every_complex_feature_carries_the_par_prefix(con):
    """Головы отключают контекст комплекса префиксом par_.

    Признак с именем вне префикса тихо просочится в голову, которая его
    отключила: так `share_par_bad` попал в E уже после того, как замер
    отверг для неё иерархию.
    """
    _row(con, "A", "P", n_bad=1)
    before = {r[0] for r in con.execute("DESCRIBE feat_object").fetchall()}
    compute.add_complex_context(con)
    after = {r[0] for r in con.execute("DESCRIBE feat_object").fetchall()}
    added = after - before
    assert added, "контекст должен что-то добавлять"
    stray = sorted(c for c in added if not c.startswith("par_"))
    assert not stray, f"вне префикса par_: {stray}"
