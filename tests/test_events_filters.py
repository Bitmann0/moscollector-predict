"""Фильтры журнала событий считаются в запросе, а не в браузере (FE-06)."""
from datetime import UTC, datetime

import pytest

API = "/api/v1/events"
TS = datetime(2026, 6, 29, 10, 0, tzinfo=UTC)


@pytest.fixture
def journal(admin, db):
    from app import models

    db.add(models.RefObject(id="T-1", level=2, parent_id=None, kind="object",
                            name="Тестовый объект Альфа"))
    db.add(models.RefObject(id="T-2", level=2, parent_id=None, kind="object",
                            name="Тестовый объект Бета"))
    db.add(models.RefChannel(id=990001, obj_id="T-1", sensor_type="Газовый датчик"))
    db.add(models.RefChannel(id=990002, obj_id="T-1", sensor_type="Датчик дыма"))
    db.add(models.RefChannel(id=990003, obj_id="T-2", sensor_type="Газовый датчик"))
    rows = [(990001, "normal", "0"), (990001, "normal", "0"), (990001, "alarm", "Обнаружен газ"),
            (990002, "normal", "Норма"), (990003, "normal", "0")]
    for n, (channel, cls, value) in enumerate(rows):
        db.add(models.Event(event_id=n, channel_id=channel, ts=TS, alarm=cls == "alarm",
                            val_raw=value, event_class=cls, row_hash=f"test-{n}"))
    db.commit()
    return admin


def total(client, **params) -> int:
    resp = client.get(API, params={"from": "2026-06-29", "to": "2026-06-29", **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["total"]


def test_hide_normal_gas_is_counted_by_server(journal):
    assert total(journal) == 5
    # Остаются тревога газового датчика и штатное событие дыма; total — по всей выборке,
    # так что пагинация видит те же числа, что и счётчик страницы.
    assert total(journal, hide_normal_gas="true") == 2


def test_object_filter_accepts_id_or_part_of_name(journal):
    assert total(journal, obj="T-2") == 1
    assert total(journal, obj="Альфа") == 4
    assert total(journal, obj="объект") == 5


def test_sensor_type_matches_substring_in_any_case(journal):
    assert total(journal, sensor_type="газ") == 4
    assert total(journal, sensor_type="Дым") == 1
