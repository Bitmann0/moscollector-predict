"""Контракт выдачи: то, на что опирается веб-интерфейс.

Схема обязана переживать переобучение модели, смену бэкенда и добавление
головы. Поэтому она проверяется отдельно от моделей.
"""
import datetime as dt

import pytest

from mkl import contract


def test_alert_id_is_deterministic():
    """Повторный расчёт тех же суток обязан дать те же идентификаторы, иначе
    потребитель наплодит дубли вместо обновления записей."""
    a = contract.make_alert_id("C", {"obj": "5122"}, dt.date(2026, 6, 30))
    b = contract.make_alert_id("C", {"obj": "5122"}, dt.date(2026, 6, 30))
    assert a == b


def test_alert_id_differs_by_day_and_entity_and_head():
    base = contract.make_alert_id("C", {"obj": "5122"}, dt.date(2026, 6, 30))
    assert base != contract.make_alert_id("C", {"obj": "5122"}, dt.date(2026, 7, 1))
    assert base != contract.make_alert_id("C", {"obj": "9999"}, dt.date(2026, 6, 30))
    assert base != contract.make_alert_id("E", {"obj": "5122"}, dt.date(2026, 6, 30))


def test_case_key_is_stable_across_days():
    """Длинный отказ порождает алерт каждые сутки. Без общего ключа диспетчер
    увидит тридцать разных происшествий вместо одного."""
    k1 = contract.make_case_key("A_link", {"ch": 120578})
    k2 = contract.make_case_key("A_link", {"ch": 120578})
    assert k1 == k2
    assert k1 != contract.make_case_key("A_link", {"ch": 120579})


def test_alert_id_does_not_depend_on_key_order():
    a = contract.make_alert_id("B", {"obj": "1", "seg": 4}, dt.date(2026, 1, 1))
    b = contract.make_alert_id("B", {"seg": 4, "obj": "1"}, dt.date(2026, 1, 1))
    assert a == b


def test_every_direction_of_the_task_has_a_title():
    """Четыре направления ТЗ плюс пометка для голов вне их."""
    for key in ("sensor_failure", "fire_risk", "unauthorised_access",
                "infrastructure_wear", "beyond_scope"):
        assert contract.DIRECTIONS[key]


def test_alert_serialises_dates_as_strings():
    a = contract.Alert(
        alert_id="x", case_key="y", schema_version=contract.SCHEMA_VERSION,
        head="C", direction="unauthorised_access",
        direction_title="Несанкционированный доступ", title="t",
        asof=dt.date(2026, 6, 30),
        valid_from=dt.datetime(2026, 7, 1), valid_to=dt.datetime(2026, 7, 2),
        horizon_hours=24, risk=0.9, rank=1, in_budget=True, above_threshold=True,
        address=contract.Address(obj="5122", picket=28.0))
    d = a.to_dict()
    assert d["asof"] == "2026-06-30"
    assert d["valid_from"].startswith("2026-07-01")
    assert d["address"]["obj"] == "5122"


def test_coverage_reports_fraction_and_survives_zero():
    c = contract.Coverage(head="C", direction="unauthorised_access",
                          entities_total=78, entities_scored=47)
    assert c.fraction == pytest.approx(47 / 78)
    empty = contract.Coverage(head="X", direction="beyond_scope",
                              entities_total=0, entities_scored=0)
    assert empty.fraction == 0.0


def test_no_data_is_distinct_from_no_risk():
    """У головы доступа состояние охраны есть на 47 объектах из 78. Молчание
    по остальным читалось бы как «всё спокойно», что неправда."""
    assert contract.STATUS_OK != contract.STATUS_NO_DATA
