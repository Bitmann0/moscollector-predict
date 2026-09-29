"""Жизненный цикл заявки на реальном приложении: граф C3, конфликты и гонки.

Гарантии перенесены из PR #1 (ветка feature/maintenance-lifecycle, коммит aa85ddf, SQLite):
переход статуса — сравнение и запись одним UPDATE, конкурентное создание не падает, на
прогноз одна активная заявка, после закрытия — новый цикл. Гонки воспроизводятся
детерминированно двумя сессиями, без потоков:
SQLite в тестах сериализует запись, и тест с потоками был бы нестабилен.
"""
import threading

import pytest
from app import models, vocab
from app.db import session_factory
from app.schemas.work_orders import WorkOrderCreate, WorkOrderTransition
from app.security import CurrentUser
from app.services import work_orders
from fastapi import HTTPException

API = "/api/v1"
ADMIN = CurrentUser(login="admin", name="admin", role="admin",
                    perms=vocab.permissions_of("admin"))
ACTIVE = {"draft", "confirmed", "in_progress"}


def _draft(client) -> str:
    return client.get(f"{API}/work-orders?status=draft").json()["items"][0]["id"]


def _forecasts(client) -> list[dict]:
    return client.get(f"{API}/forecasts").json()["items"]


def _history(client, order_id: str) -> list[tuple[str | None, str]]:
    card = client.get(f"{API}/work-orders/{order_id}").json()
    return [(h["from_status"], h["to_status"]) for h in card["history"]]


def _move(client, order_id: str, expected: str, status: str, **extra):
    return client.patch(f"{API}/work-orders/{order_id}",
                        json={"expected_status": expected, "status": status, **extra})


def _active_orders_of(client, forecast_id: str) -> list[str]:
    return [o["id"] for o in client.get(f"{API}/work-orders").json()["items"]
            if forecast_id in o["forecast_ids"] and o["status"] in ACTIVE]


def test_full_lifecycle_writes_history(ran, admin):
    order = _draft(admin)
    for expected, status in [("draft", "confirmed"), ("confirmed", "in_progress"),
                             ("in_progress", "completed")]:
        resp = _move(admin, order, expected, status, reason=f"шаг {status}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == status
    assert _history(admin, order) == [(None, "draft"), ("draft", "confirmed"),
                                      ("confirmed", "in_progress"),
                                      ("in_progress", "completed")]
    assert _move(admin, order, "completed", "cancelled").status_code == 422


def test_conflicts_do_not_touch_history(ran, admin):
    order = _draft(admin)
    assert _move(admin, order, "draft", "confirmed").status_code == 200
    stale = _move(admin, order, "draft", "cancelled")
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == {"code": "status_conflict", "current_status": "confirmed"}
    skip = _move(admin, order, "confirmed", "completed")
    assert skip.status_code == 422, skip.text
    assert skip.json()["detail"] == "work_order_transition_not_allowed"
    assert _history(admin, order) == [(None, "draft"), ("draft", "confirmed")]
    assert _move(admin, "missing", "draft", "confirmed").status_code == 404


def test_dispatcher_cannot_progress(ran, login):
    dispatcher = login("dispatcher")
    order = _draft(dispatcher)
    assert _move(dispatcher, order, "draft", "confirmed").status_code == 200
    resp = _move(dispatcher, order, "confirmed", "in_progress")
    assert resp.status_code == 403, resp.text
    assert _history(dispatcher, order)[-1] == ("draft", "confirmed")


def test_stale_read_transition_returns_409(ran, admin):
    """Сессия прочитала draft, другая успела перевести в confirmed: запись не проходит."""
    order = _draft(admin)
    stale = session_factory()()
    try:
        # Ссылку держим: карта идентичности слабая, без неё get повторит SELECT
        # и прочитает уже новый статус.
        held = stale.get(models.WorkOrder, order)
        assert held.status == "draft"
        assert _move(admin, order, "draft", "confirmed").status_code == 200
        with pytest.raises(HTTPException) as error:
            work_orders.transition(stale, order, WorkOrderTransition(
                expected_status="draft", status="cancelled"), ADMIN)
    finally:
        stale.close()
    assert error.value.status_code == 409
    assert error.value.detail == {"code": "status_conflict", "current_status": "confirmed"}
    assert _history(admin, order) == [(None, "draft"), ("draft", "confirmed")]
    assert admin.get(f"{API}/work-orders/{order}").json()["status"] == "confirmed"


def test_concurrent_manual_create_returns_same_order(ran, admin):
    """Второй запрос проверил, что заявки нет, а первый вставил её раньше: не 500."""
    forecast = next(f["id"] for f in _forecasts(admin) if f["work_order_id"] is None)
    body = WorkOrderCreate(forecast_ids=[forecast])
    first, second = session_factory()(), session_factory()()
    real_get = second.get
    won: dict = {}

    def get_racing(entity, ident, **kwargs):
        found = real_get(entity, ident, **kwargs)
        if entity is models.WorkOrder and not won:
            # Первый запрос успевает вставить заявку между проверкой и вставкой второго.
            won["card"] = work_orders.create(first, body, ADMIN)
        return found

    second.get = get_racing
    try:
        card = work_orders.create(second, body, ADMIN)
    finally:
        first.close()
        second.close()
    assert card.id == won["card"].id and card.id.startswith("WO-")
    assert _history(admin, card.id) == [(None, "draft")]


def test_postgres_forecast_lock_serializes_transactions(db):
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("нужен PostgreSQL для проверки advisory lock")
    first, second = session_factory()(), session_factory()()
    acquired = threading.Event()
    started = threading.Event()

    def contender():
        started.set()
        work_orders.lock_forecasts(second, {"shared-forecast"})
        acquired.set()
        second.rollback()

    thread = threading.Thread(target=contender)
    try:
        work_orders.lock_forecasts(first, {"shared-forecast"})
        thread.start()
        assert started.wait(2)
        assert not acquired.wait(0.1)
        first.commit()
        assert acquired.wait(2)
    finally:
        first.rollback()
        thread.join(timeout=2)
        second.rollback()
        first.close()
        second.close()


def test_manual_create_reuses_active_ml_order(ran, admin):
    forecast = next(f for f in _forecasts(admin) if f["work_order_id"])
    resp = admin.post(f"{API}/work-orders", json={"forecast_ids": [forecast["id"]]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == forecast["work_order_id"]
    assert resp.json()["created_by"] == "system"
    assert _active_orders_of(admin, forecast["id"]) == [forecast["work_order_id"]]


def test_new_cycle_after_cancel(ran, admin):
    forecast = next(f for f in _forecasts(admin) if f["work_order_id"])
    ml_order = forecast["work_order_id"]
    assert _move(admin, ml_order, "draft", "cancelled", reason="ложный прогноз").status_code \
        == 200
    created = admin.post(f"{API}/work-orders", json={"forecast_ids": [forecast["id"]]})
    assert created.status_code == 201, created.text
    card = created.json()
    assert card["id"].startswith("WO-") and card["created_by"] == "admin"
    assert card["status"] == "draft"
    again = admin.post(f"{API}/work-orders", json={"forecast_ids": [forecast["id"]]}).json()
    assert again["id"] == card["id"]
    assert _active_orders_of(admin, forecast["id"]) == [card["id"]]


def test_new_cycle_after_manual_close(ran, admin):
    """Закрытая ручная заявка не блокирует новую: тот же набор прогнозов открывает цикл 2."""
    forecast = next(f["id"] for f in _forecasts(admin) if f["work_order_id"] is None)
    body = {"forecast_ids": [forecast]}
    closed = admin.post(f"{API}/work-orders", json=body).json()["id"]
    assert _move(admin, closed, "draft", "cancelled").status_code == 200
    created = admin.post(f"{API}/work-orders", json=body)
    assert created.status_code == 201, created.text
    card = created.json()
    assert card["id"] == f"{closed}-2" and card["status"] == "draft"
    assert _history(admin, card["id"]) == [(None, "draft")]
    assert admin.post(f"{API}/work-orders", json=body).json()["id"] == card["id"]
    assert admin.get(f"{API}/work-orders/{closed}").json()["status"] == "cancelled"
    assert _active_orders_of(admin, forecast) == [card["id"]]


def test_order_inserted_after_active_check_is_reused(ran, admin, monkeypatch):
    """Заявку на тот же набор завели уже после проверки активных: отдаём её, а не цикл 2."""
    forecast = next(f["id"] for f in _forecasts(admin) if f["work_order_id"] is None)
    body = {"forecast_ids": [forecast]}
    first = admin.post(f"{API}/work-orders", json=body).json()["id"]
    monkeypatch.setattr(work_orders, "_active_order_for", lambda db, forecast_ids: None)
    assert admin.post(f"{API}/work-orders", json=body).json()["id"] == first
    assert _active_orders_of(admin, forecast) == [first]
