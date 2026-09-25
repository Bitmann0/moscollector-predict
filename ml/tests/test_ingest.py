import pytest

from mkl import ingest


@pytest.mark.parametrize("name,expected", [
    ("Темп. ВШ ПК88,5", 88.5),
    ("ТД ПК86-85", 86.0),
    ("КД АВ ПК28", 28.0),
    ("[Охранная зона]0: 0: ", None),
    (None, None),
])
def test_parse_picket(name, expected):
    assert ingest.parse_picket(name) == expected


@pytest.mark.parametrize("tag,expected", [
    ("15-11.1.131.2.", "15-11"),
    ("847-1.1.4096.4095.", "847-1"),
    (None, None),
])
def test_parse_object(tag, expected):
    assert ingest.parse_object(tag) == expected


def test_discovers_new_journal_years_without_code_change(tmp_path):
    for name in ("ext-journal-2025.csv", "ext-journal-2027.csv", "notes.csv"):
        (tmp_path / name).touch()
    assert ingest.discover_journal_years(tmp_path) == [2025, 2027]


# --- дедупликация -----------------------------------------------------------
#
# Корень пайплайна: всё, что здесь потеряно, не восстановит ни один признак.
# Прежняя версия дедуплицировала по `ид_события`, полагая его ключом. Ключом он
# не является — в 2021-2023 идентификатор переиспользуется разными событиями,
# и такая дедупликация удаляла 1 669 268 настоящих событий из 311 млн.

_HEADER = "ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика\n"


def _write_journal(tmp_path, rows):
    src = tmp_path / "ext-journal-2099.csv"
    src.write_text(_HEADER + "".join(rows), encoding="utf-8")
    return src


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """Приём одного синтетического года в изолированных каталогах."""
    import polars as pl
    from mkl.config import Paths

    raw, interim = tmp_path / "raw", tmp_path / "interim"
    raw.mkdir(), interim.mkdir()
    monkeypatch.setattr(ingest, "PATHS", Paths(raw=raw, interim=interim))
    pl.DataFrame({
        "ch": [1, 2], "sys": ["s", "s"], "stype": ["Датчик дыма"] * 2,
        "tag": ["t", "t"], "sname": ["n", "n"], "obj": ["A", "A"],
        "obj_parent": ["P", "P"], "obj_kind": ["guardObject"] * 2,
        "obj_level": [3, 3], "tag_prefix": ["t", "t"], "picket": [1.0, 2.0],
    }).write_parquet(interim / "channels.parquet")

    def run(rows):
        _write_journal(raw, rows)
        ingest.build_events([2099])
        return pl.read_parquet(interim / "events_year=2099.parquet")

    return run


def test_exact_duplicate_rows_are_collapsed(journal):
    """Полный повтор строки — дубль выгрузки, он лишний."""
    row = "100,1,2099-01-01,10:00:00,f,Норма\n"
    got = journal([row, row, row])
    assert got.height == 1


def test_events_sharing_an_id_are_all_kept(journal):
    """Один `ид_события` у разных событий — не дубль, а переиспользование
    идентификатора. Прежняя дедупликация оставляла здесь одну строку из трёх.
    """
    got = journal([
        "100,1,2099-01-01,10:00:00,f,Норма\n",
        "100,2,2099-01-01,10:00:00,f,Норма\n",
        "100,1,2099-01-01,11:30:00,t,Неисправен\n",
    ])
    assert got.height == 3
    assert sorted(got["ch"].to_list()) == [1, 1, 2]


def test_same_id_and_moment_but_different_value_is_kept(journal):
    got = journal([
        "100,1,2099-01-01,10:00:00,f,Норма\n",
        "100,1,2099-01-01,10:00:00,t,Обесточен\n",
    ])
    assert got.height == 2


def test_ingest_is_deterministic_across_runs(journal):
    """Три пересборки одного года давали три разных датасета: `DISTINCT ON`
    без `ORDER BY` выбирал выжившую строку недетерминированно."""
    rows = [f"{i % 7},{i % 2 + 1},2099-01-0{i % 9 + 1},1{i % 10}:00:00,f,Норма\n"
            for i in range(200)]
    runs = [journal(rows).sort(["event_id", "ch", "ts", "val_raw"]) for _ in range(3)]
    assert runs[0].equals(runs[1]) and runs[1].equals(runs[2])


def test_header_rows_inside_the_file_are_dropped(journal):
    got = journal([
        "100,1,2099-01-01,10:00:00,f,Норма\n",
        _HEADER,
        "101,1,2099-01-01,11:00:00,f,Норма\n",
    ])
    assert got.height == 2
