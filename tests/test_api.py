"""REST API — то, через что веб-интерфейс получает прогнозы.

Проверяется контракт, а не модели: маршруты, фильтры, коды ошибок. Сами числа
живут в других тестах.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from mkl import api, contract, workorders
from mkl.contract import Address, Alert


def _alert(head="C", risk=0.9, obj="5122", direction="unauthorised_access",
           in_budget=True, day=dt.date(2026, 6, 30)):
    ent = {"obj": obj}
    start = dt.datetime.combine(day, dt.time()) + dt.timedelta(days=1)
    return Alert(
        alert_id=contract.make_alert_id(head, ent, day),
        case_key=contract.make_case_key(head, ent),
        schema_version=contract.SCHEMA_VERSION, head=head, direction=direction,
        direction_title=contract.DIRECTIONS[direction], title="t",
        asof=day, valid_from=start, valid_to=start + dt.timedelta(hours=24),
        horizon_hours=24, risk=risk, rank=1, in_budget=in_budget,
        above_threshold=True, address=Address(obj=obj, picket=28.0),
        factors=[{"feature": "n_intrusion", "label": "срабатывания датчиков",
                  "contribution": 0.4}])


@pytest.fixture
def client(monkeypatch):
    fake = [_alert(), _alert(head="D", risk=0.5, obj="5003",
                             direction="infrastructure_wear")]
    monkeypatch.setattr(api.service, "daily_alerts",
                        lambda **kw: list(fake))
    monkeypatch.setattr(api.service, "coverage", lambda asof=None: [
        contract.Coverage(head="C", direction="unauthorised_access",
                          entities_total=78, entities_scored=39,
                          reason="только там, где известно состояние охраны")])
    api.reset_cache()
    return TestClient(api.app)


def test_health_reports_trained_heads(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["schema_version"] == contract.SCHEMA_VERSION


def test_alerts_are_returned_with_address_and_factors(client):
    got = client.get("/api/v1/alerts").json()
    assert got["count"] == 2
    a = got["alerts"][0]
    assert a["address"]["obj"] == "5122"
    assert a["address"]["picket"] == 28.0
    assert a["factors"][0]["label"] == "срабатывания датчиков"
    assert a["horizon_hours"] == 24


def test_alerts_filter_by_direction(client):
    got = client.get("/api/v1/alerts?direction=infrastructure_wear").json()
    assert got["count"] == 1 and got["alerts"][0]["head"] == "D"


def test_alerts_limit_is_reported_separately_from_count(client):
    """Потребитель должен видеть, что список урезан, а не думать, что это всё."""
    got = client.get("/api/v1/alerts?limit=1").json()
    assert got["count"] == 2 and got["returned"] == 1


def test_alert_card_is_addressable_by_its_id(client):
    first = client.get("/api/v1/alerts").json()["alerts"][0]
    r = client.get(f"/api/v1/alerts/{first['alert_id']}")
    assert r.status_code == 200 and r.json()["alert_id"] == first["alert_id"]


def test_unknown_alert_gives_404(client):
    assert client.get("/api/v1/alerts/нетакого").status_code == 404


def test_stale_pilot_data_is_conflict_not_empty_success(monkeypatch):
    monkeypatch.setattr(api.service, "daily_alerts",
                        lambda **kw: (_ for _ in ()).throw(
                            ValueError("latest feature day is stale")))
    api.reset_cache()
    got = TestClient(api.app).get("/api/v1/alerts")
    assert got.status_code == 409
    assert "stale" in got.json()["detail"]


def test_alert_cache_invalidates_on_new_data_or_day(monkeypatch):
    calls = []
    generation = [1]
    monkeypatch.setattr(api, "_cache_generation", lambda: generation[0])
    monkeypatch.setattr(api.service, "daily_alerts",
                        lambda **kw: calls.append(1) or [])
    api.reset_cache()
    client = TestClient(api.app)
    assert client.get("/api/v1/alerts").status_code == 200
    assert client.get("/api/v1/alerts").status_code == 200
    assert len(calls) == 1
    generation[0] = 2
    assert client.get("/api/v1/alerts").status_code == 200
    assert len(calls) == 2


def test_coverage_shows_the_gap_not_a_full_bar(client):
    """Доля покрытия единицей ровно там, где разрыв и надо показать, — это
    дефект, который уже случался: знаменатель брался из среза, а не справочника.
    """
    got = client.get("/api/v1/coverage").json()[0]
    assert got["entities_total"] == 78 and got["entities_scored"] == 39
    assert got["fraction"] < 1.0 and got["reason"]


def test_directions_cover_all_four_of_the_task(client):
    got = client.get("/api/v1/directions").json()
    keys = {d["direction"] for d in got}
    assert {"sensor_failure", "fire_risk", "unauthorised_access",
            "infrastructure_wear"} <= keys


def test_work_orders_group_alerts_by_object(client):
    got = client.get("/api/v1/work-orders").json()
    assert got["count"] == 2, "два объекта — две заявки"
    assert all(o["obj"] for o in got["orders"])


def test_work_order_is_addressable_and_carries_rationale(client):
    first = client.get("/api/v1/work-orders").json()["orders"][0]
    r = client.get(f"/api/v1/work-orders/{first['order_id']}")
    assert r.status_code == 200
    assert r.json()["rationale"] == ["срабатывания датчиков"]


def test_work_orders_filter_by_priority(client):
    got = client.get(f"/api/v1/work-orders?priority={workorders.PRIORITY_URGENT}").json()
    assert all(o["priority"] == workorders.PRIORITY_URGENT for o in got["orders"])
