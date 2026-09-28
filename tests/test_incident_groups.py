"""Группы аварий и серии ППР/ТО по ответам заказчика 28.09 (analysis/qa_customer_2026-09-28.md).

Классы и группы проверяются через classify, серии и фильтр — через приём и журнал API:
подсказку серии ставит приём пачки, а не classify. Тексты подсказок записаны строками,
а не константами semantics: тест должен падать, если текст для диспетчера поменялся.
"""
from datetime import datetime

import pytest
from app.services import notifications, semantics
from app.services.helpers import MSK

API = "/api/v1"
MONDAY = "2026-06-29"
SATURDAY = "2026-06-27"
FIRE_SERIES = "вероятно, ППР или ТО: серия из {} извещателей за 10 минут"
GAS_SERIES = "вероятно, ППР или ТО: серия из {} газоанализаторов за 10 минут"
GAS_WINDOW = "вероятно, ППР или ТО: газ в будни с 9:00 до 14:59"
# Ночь: вне окна газа 9:00–14:59 и вне рабочего времени серий.
NIGHT = datetime(2026, 6, 29, 2, 0, tzinfo=MSK)


@pytest.mark.parametrize(("sensor_type", "value", "group"), [
    ("Датчик дыма", "Обнаружен дым", "fire"),
    ("Тепловой датчик", "Не замкнут", "fire"),
    ("Ручной извещатель", "Не замкнут", "fire"),
    ("Состояние УИР-Р", "Рычаг сдернут", "fire"),
    ("Датчик затопления", "Не замкнут", "flood"),
    ("Состояние насоса", "Затоплен", "flood"),
    ("Газовый датчик", "Обнаружен газ", "gas"),
    ("КД Дверь", "Не замкнут", "intrusion"),
    ("КД Люк", "Не замкнут", "intrusion"),
    ("КД АВ", "Не замкнут", "intrusion"),
    ("Стекло", "Не замкнут", "intrusion"),
    ("Датчик движения", "Обнаружено движение", "intrusion"),
    ("Датчик температуры", "Температура выше 40ºC", "temperature"),
    ("Датчик температуры", "Температура ниже 3ºC", "temperature"),
])
def test_alarm_of_incident_group_is_critical(sensor_type, value, group):
    assert semantics.classify(sensor_type, value, None, True, ts=NIGHT) == (
        "critical", None, group)


@pytest.mark.parametrize(("sensor_type", "value"), [
    ("Состояние насоса", "Обесточен"),
    ("Датчик дыма", "Отключено устройство"),
    ("ИБП", "Питание от батарей"),
    ("Датчик дыма", "Не замкнут"),         # сработка дымового — «Обнаружен дым»
    ("Состояние УИР-Р", "Не замкнут"),      # сработка УИР-Р — «Рычаг сдернут»
    (None, "Не замкнут"),                   # канала нет в справочнике: группу не угадать
])
def test_alarm_outside_groups_stays_alarm(sensor_type, value):
    assert semantics.classify(sensor_type, value, None, True, ts=NIGHT) == ("alarm", None, None)


def test_faults_and_unflagged_hazards_keep_their_class():
    assert semantics.classify("Датчик дыма", "Неисправен", None, True) == ("fault", None, None)
    # без флага группа не ставится: правило #42 — предупреждение
    assert semantics.classify("Датчик дыма", "Обнаружен дым", None, False, ts=NIGHT) == (
        "warning", "в СМВУ без признака тревоги", None)
    assert semantics.classify("КД Дверь", "Не замкнут", None, False) == ("normal", None, None)
    # метан по порогам — как раньше, без группы
    assert semantics.classify("Газовый датчик", "6", 6.0, True) == ("critical", None, None)


@pytest.fixture
def site(admin, db):
    """Комплекс T-C: объект T-1 с шестью тепловыми извещателями и газоанализаторами
    на объектах T-1 и T-2 — «подряд вдоль коллектора» одного комплекса."""
    from app import models

    db.add(models.RefObject(id="T-C", level=2, parent_id=None, kind="controlHouse",
                            name="Тестовый комплекс"))
    for obj in ("T-1", "T-2"):
        db.add(models.RefObject(id=obj, level=3, parent_id="T-C", kind="controlHouse",
                                name=f"Объект {obj}"))
    for n in range(6):
        db.add(models.RefChannel(id=991000 + n, obj_id="T-1", sensor_type="Тепловой датчик"))
    for n in range(4):
        db.add(models.RefChannel(id=992000 + n, obj_id="T-1" if n < 2 else "T-2",
                                 sensor_type="Газовый датчик"))
    db.commit()
    return admin


def row(event_id: int, channel: int, time: str, value: str, day: str = MONDAY) -> dict:
    return {"ид_события": event_id, "ид_канала_данных": channel, "дата": day,
            "время": time, "тревожное": "t", "значение_датчика": value}


def hints(client, group: str) -> list[str | None]:
    """Подсказки событий группы по времени; заодно проверяет, что фильтр отдал только её."""
    resp = client.get(f"{API}/events", params={"incident_group": group, "page_size": 1000})
    assert resp.status_code == 200, resp.text
    items = sorted(resp.json()["items"], key=lambda i: i["ts"])
    assert {(i["event_class"], i["incident_group"]) for i in items} == {("critical", group)}
    return [item["hint"] for item in items]


def fire_rows(detectors: int, day: str = MONDAY) -> list[dict]:
    """Извещатели T-1 срабатывают через 2 минуты: пятый — через 8 минут после первого."""
    return [row(n, 991000 + n, f"10:{2 * n:02d}:00", "Не замкнут", day)
            for n in range(detectors)]


def test_five_fire_detectors_in_ten_minutes_mark_all_five(site):
    for item in fire_rows(5):   # по одному в пачке, как в потоке: серию видно на пятом
        assert site.post(f"{API}/ingest/events", json=[item]).json()["accepted"] == 1
    assert hints(site, "fire") == [FIRE_SERIES.format(5)] * 5


def test_four_fire_detectors_are_not_a_series(site):
    site.post(f"{API}/ingest/events", json=fire_rows(4))
    assert hints(site, "fire") == [None] * 4


def test_fire_series_outside_working_time_has_no_hint(site):
    site.post(f"{API}/ingest/events", json=fire_rows(5, day=SATURDAY))
    assert hints(site, "fire") == [None] * 5


def test_series_hint_grows_with_the_series(site):
    site.post(f"{API}/ingest/events", json=fire_rows(5))
    site.post(f"{API}/ingest/events", json=[row(5, 991005, "10:09:00", "Не замкнут")])
    assert hints(site, "fire") == [FIRE_SERIES.format(6)] * 6


def test_dashboard_counts_series_as_planned_like(site):
    site.post(f"{API}/ingest/events", json=fire_rows(5, day="2026-06-30") + [
        row(9, 991005, "03:00:00", "Не замкнут", day="2026-06-30")])
    summary = site.get(f"{API}/dashboard/summary").json()
    assert (summary["alarms_24h"], summary["planned_like_alarms_24h"]) == (6, 5)


def test_gas_detectors_along_complex_form_series(site):
    # 8:30 — рабочее время, но до окна газа 9:00–14:59: подсказку даёт только серия.
    rows = [row(10 + n, 992000 + n, f"08:3{n}:00", "Обнаружен газ") for n in range(4)]
    site.post(f"{API}/ingest/events", json=rows[:3])
    assert hints(site, "gas") == [None] * 3
    site.post(f"{API}/ingest/events", json=rows[3:])
    assert hints(site, "gas") == [GAS_SERIES.format(4)] * 4


def test_gas_on_weekday_daytime_gets_ppr_hint(site):
    site.post(f"{API}/ingest/events", json=[row(20, 992000, "10:00:00", "Обнаружен газ"),
                                            row(21, 992001, "16:00:00", "Обнаружен газ")])
    assert hints(site, "gas") == [GAS_WINDOW, None]


def test_incident_group_filter_and_notification_payload(site, monkeypatch):
    published = []
    monkeypatch.setattr(notifications.broker, "publish",
                        lambda kind, payload, **kw: published.append((kind, payload, kw)))
    site.post(f"{API}/ingest/events", json=[
        row(30, 991000, "03:00:00", "Не замкнут"),
        row(31, 992000, "03:00:00", "Обнаружен газ"),
        row(32, 992001, "03:01:00", "Отключено устройство"),
    ])
    items = site.get(f"{API}/events", params={"incident_group": "gas"}).json()["items"]
    assert [(i["sensor_event"], i["event_class"], i["incident_group"]) for i in items] == [
        ("Обнаружен газ", "critical", "gas")]
    assert site.get(f"{API}/events", params={"incident_group": "fire"}).json()["total"] == 1
    assert site.get(f"{API}/events", params={"incident_group": "smoke"}).status_code == 422
    payloads = sorted((p["event_id"], p["incident_group"], kw["severity"])
                      for kind, p, kw in published if kind == "event.alarm")
    assert payloads == [(30, "fire", "critical"), (31, "gas", "critical"),
                        (32, None, "warning")]
