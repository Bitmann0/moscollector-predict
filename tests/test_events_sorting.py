"""Сортировка журнала событий по каждой колонке формы Приложения 2 ТЗ (FE-06).

Значение датчика у каждого события своё, поэтому порядок проверяется по нему.
Внутри равных значений колонки события идут от новых к старым.
"""
from datetime import UTC, datetime, timedelta

import pytest

API = "/api/v1/events"
DAY = {"from": "2026-06-29", "to": "2026-06-29"}
TS = datetime(2026, 6, 29, 10, 0, tzinfo=UTC)

# (канал, класс, значение, число); минута события — номер строки.
ROWS = [
    (990001, "normal", "0.5", 0.5),              # 0  Бета,  газ
    (990002, "alarm", "Обнаружен дым", None),    # 1  Альфа, дым
    (990003, "fault", "-60", -60.0),             # 2  Гамма, температура
    (990004, "critical", "12", 12.0),            # 3  Альфа, газ
    (990003, "warning", "2", 2.0),               # 4  Гамма, температура
    (990002, "normal", "Дыма нет", None),        # 5  Альфа, дым
    (990001, "service", "Выключен", None),       # 6  Бета,  газ
]
VALUE = [row[2] for row in ROWS]


@pytest.fixture
def journal(admin, db):
    from app import models

    for obj_id, name in [("T-1", "Тестовый объект Альфа"), ("T-2", "Тестовый объект Бета"),
                         ("T-3", "Тестовый объект Гамма")]:
        db.add(models.RefObject(id=obj_id, level=2, parent_id=None, kind="object", name=name))
    for channel, obj_id, sensor in [(990001, "T-2", "Газовый датчик"), (990002, "T-1", "Датчик дыма"),
                                    (990003, "T-3", "Датчик температуры"),
                                    (990004, "T-1", "Газовый датчик")]:
        db.add(models.RefChannel(id=channel, obj_id=obj_id, sensor_type=sensor))
    for n, (channel, cls, raw, num) in enumerate(ROWS):
        db.add(models.Event(event_id=n, channel_id=channel, ts=TS + timedelta(minutes=n),
                            alarm=cls in ("alarm", "critical"), val_raw=raw, val_num=num,
                            event_class=cls, row_hash=f"sort-{n}"))
    db.commit()
    return admin


def order(client, **params) -> list[int]:
    """Номера строк ROWS в порядке ответа."""
    resp = client.get(API, params={**DAY, **params})
    assert resp.status_code == 200, resp.text
    return [VALUE.index(item["sensor_event"]) for item in resp.json()["items"]]


@pytest.mark.parametrize(("sort", "direction", "expected"), [
    ("ts", "desc", [6, 5, 4, 3, 2, 1, 0]),
    ("ts", "asc", [0, 1, 2, 3, 4, 5, 6]),
    # Альфа (5, 3, 1) → Бета (6, 0) → Гамма (4, 2)
    ("object", "asc", [5, 3, 1, 6, 0, 4, 2]),
    ("object", "desc", [4, 2, 6, 0, 5, 3, 1]),
    # Газовый датчик → Датчик дыма → Датчик температуры
    ("sensor_type", "asc", [6, 3, 0, 5, 1, 4, 2]),
    ("sensor_type", "desc", [4, 2, 5, 1, 6, 3, 0]),
    # Числа по величине (−60 < 0,5 < 2 < 12, а не «12» < «2» как у строк), текст после чисел
    ("sensor_event", "asc", [2, 0, 4, 3, 6, 5, 1]),
    ("sensor_event", "desc", [3, 4, 0, 2, 1, 5, 6]),
    # Порядок словаря C3: норма, предупреждение, тревога, критическое, неисправность, служебное
    ("event_class", "asc", [5, 0, 4, 1, 3, 2, 6]),
    ("event_class", "desc", [6, 2, 3, 1, 4, 5, 0]),
])
def test_each_column_sorts_both_ways(journal, sort, direction, expected):
    assert order(journal, sort=sort, order=direction) == expected


def test_default_is_newest_first(journal):
    assert order(journal) == order(journal, sort="ts", order="desc")


def test_sort_works_with_filters_and_pages(journal):
    # Штатный газ (строка 0) скрыт; объекты по алфавиту, страницы по две строки.
    pages = [order(journal, sort="object", order="asc", hide_normal_gas="true",
                   page=page, page_size=2) for page in (1, 2, 3)]
    assert pages == [[5, 3], [1, 6], [4, 2]]
    assert order(journal, sort="event_class", order="desc", obj="T-1") == [3, 1, 5]
    assert order(journal, sort="sensor_event", order="desc", sensor_type="газ",
                 page=2, page_size=2) == [6]


def test_total_does_not_depend_on_sort(journal):
    totals = {journal.get(API, params={**DAY, "sort": sort, "hide_normal_gas": "true"}).json()["total"]
              for sort in ("ts", "object", "sensor_type", "sensor_event", "event_class")}
    assert totals == {6}


def test_column_sort_needs_range_of_seven_days(journal):
    week = {"from": "2026-06-23", "to": "2026-06-29"}
    assert journal.get(API, params={**week, "sort": "object"}).status_code == 200
    for params in ({}, {"from": "2026-06-29"}, {"to": "2026-06-29"},
                   {"from": "2026-06-22", "to": "2026-06-29"}):
        resp = journal.get(API, params={**params, "sort": "sensor_type"})
        assert resp.status_code == 422, params
        assert resp.json()["detail"] == "sort_needs_range"
    # По времени журнал сортируется за любой период: у ts есть индекс.
    assert journal.get(API, params={"sort": "ts", "order": "asc"}).status_code == 200


def test_unknown_sort_is_rejected(journal):
    assert journal.get(API, params={**DAY, "sort": "val_raw"}).status_code == 422
    assert journal.get(API, params={**DAY, "order": "up"}).status_code == 422
