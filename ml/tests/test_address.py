"""Человеческий адрес алерта.

Диспетчер работает названиями, а не идентификаторами. Названия живут в
справочниках и приклеиваются на границе контракта, а не в фичесторе: строки
высокой кардинальности на 4,27 млн строк стоят места и меняют отпечаток набора,
а идентификатор объекта как признак — прямая утечка.
"""
import polars as pl
import pytest

from mkl import address


@pytest.fixture
def ref(tmp_path, monkeypatch):
    df = pl.DataFrame({
        "ch": [1, 2, 3, 4],
        "obj": ["10", "10", "20", "20"],
        "obj_name": ["объект Альфа"] * 2 + ["объект Бета"] * 2,
        "obj_parent": ["1", "1", "2", "2"],
        "obj_parent_name": ["комплекс Один"] * 2 + ["комплекс Два"] * 2,
        "obj_kind": ["guardObject"] * 2 + ["controlHouse"] * 2,
        "sname": ["КД АВ ПК28", "Темп. ВШ", "ТД ПК5", "Насос"],
        "tag": ["15-11.1", "15-11.2", "20-1.1", "20-1.2"],
        "stype": ["КД АВ", "Датчик температуры", "ТД", "Состояние насоса"],
        "picket": [28.0, None, 5.0, None],
    })
    path = tmp_path / "channels.parquet"
    df.write_parquet(path)

    class _P:
        interim = tmp_path
    monkeypatch.setattr(address, "PATHS", _P)
    address.reset_cache()
    yield
    address.reset_cache()


def test_channel_address_carries_names_not_only_ids(ref):
    got = address.for_channel(1)
    assert got["obj_name"] == "объект Альфа"
    assert got["obj_parent_name"] == "комплекс Один"
    assert got["sensor_name"] == "КД АВ ПК28"
    assert got["picket_label"] == "ПК 28"


def test_object_kind_is_translated(ref):
    assert address.for_channel(1)["obj_kind_ru"] == "охранная зона"
    assert address.for_channel(3)["obj_kind_ru"] == "диспетчерский пункт"


def test_channel_outside_the_reference_is_marked_not_blanked(ref):
    """1 142 канала из 12 627 есть в журналах и отсутствуют в справочнике.
    Пустая строка в интерфейсе выглядела бы как отсутствие проблемы."""
    got = address.for_channel(999)
    assert got["address_known"] is False
    assert got["obj_name"] == address.UNKNOWN


def test_missing_picket_is_stated_not_guessed(ref):
    """Пикета нет у 847 каналов из 11 485."""
    got = address.for_channel(2)
    assert got["picket"] is None
    assert got["picket_label"] == address.UNKNOWN_PICKET


def test_segment_zero_is_resolved_into_four_distinct_cases(ref):
    """Участок считался как floor(пикет/10) с подстановкой нуля вместо
    пропуска, отчего «ПК 0-10» и «пикет неизвестен» сливались: на реальных
    данных из 994 936 строк с нулевым участком у 809 934 пикета нет вовсе.

    Первая версия правки решала по объекту и потому была честна ровно там, где
    слияния и не было: если у объекта есть хоть один канал с настоящим пикетом
    в нулевой корзине, печаталось уверенное «ПК 0-10». Таких смешанных объектов
    28 из 78, и на них подпись утверждала начало коллектора для каналов, у
    которых пикета нет вовсе.
    """
    from mkl.config import SEG_UNKNOWN

    # объект 10: пикет 28 уходит во второй участок, в нулевом только беспикетный
    assert address.segment_label("10", 0) == address.NO_PICKET_SEGMENT
    # объект 20: в нулевой корзине и настоящий ПК 5, и беспикетный канал
    assert address.segment_label("20", 0) == address.MIXED_SEGMENT
    # отдельная корзина беспикетных из нового набора признаков
    assert address.segment_label("20", SEG_UNKNOWN) == address.NO_PICKET_SEGMENT
    # обычный участок подписывается как есть
    assert address.segment_label("10", 2) == "ПК 20–30"


def test_known_segment_is_labelled_by_kilometre_marks(ref):
    assert address.segment_label("10", 8) == "ПК 80\u201390"


def test_object_address_works_without_a_channel(ref):
    got = address.for_object("20")
    assert got["obj_name"] == "объект Бета"
    assert got["obj_parent_name"] == "комплекс Два"


def test_describe_prefers_channel_over_object(ref):
    got = address.describe(obj="20", ch=1)
    assert got["obj_name"] == "объект Альфа", "канал точнее объекта"


@pytest.mark.skipif(not (address.PATHS.interim / "channels.parquet").exists(),
                    reason="нужен справочник каналов из данных заказчика")
def test_real_reference_has_a_name_for_every_object():
    """Проверка на настоящих данных: адрес обязан находиться для всех."""
    df = address.channels()
    assert df["obj_name"].null_count() == 0
    assert df["obj_parent_name"].null_count() == 0
