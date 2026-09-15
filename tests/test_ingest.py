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
