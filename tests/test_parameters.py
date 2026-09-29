"""Настраиваемые параметры продукта (ТЗ §18, ML2-13): /settings/parameters, применение
при приёме и в дневном расчёте, пересчёт истории, блокировка на стенде и аудит."""
import copy
import re
from pathlib import Path

import pytest
from app import models
from app.config import get_settings
from app.schemas.parameters import VERIFIED_LIMITS
from app.services import daily_run, notifications, parameters
from conftest import MONDAY, TUESDAY, alert_id, build_score
from sqlalchemy import event, func, select

API = "/api/v1"
URL = f"{API}/settings/parameters"
ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-06-29"     # понедельник
GAS_CH, UPS_CH, HEAT_CH = 995001, 995002, 995003


def values(**changes) -> dict:
    """Проверенные параметры с заменой полей: values(gas__alarm_pct=0.5)."""
    body = copy.deepcopy(parameters.VERIFIED.model_dump(mode="json"))
    for path, value in changes.items():
        *parents, leaf = path.split("__")
        node = body
        for key in parents:
            node = node[key]
        node[leaf] = value
    return body


def put(client, version: int = 0, **changes):
    return client.put(URL, json={"expected_version": version, "values": values(**changes)})


@pytest.fixture
def plant(admin, db):
    """Объект P-1 комплекса P-C: газовый датчик, ИБП и датчик температуры."""
    db.add(models.RefObject(id="P-C", level=2, parent_id=None, kind="controlHouse", name="К"))
    db.add(models.RefObject(id="P-1", level=3, parent_id="P-C", kind="controlHouse", name="О"))
    for channel, sensor_type in ((GAS_CH, "Газовый датчик"), (UPS_CH, "ИБП"),
                                 (HEAT_CH, "Датчик температуры")):
        db.add(models.RefChannel(id=channel, obj_id="P-1", sensor_type=sensor_type))
    db.commit()
    return admin


def row(event_id: int, channel: int, value: str, *, time: str = "03:00:00",
        alarm: str = "f", day: str = DAY) -> dict:
    return {"ид_события": event_id, "ид_канала_данных": channel, "дата": day,
            "время": time, "тревожное": alarm, "значение_датчика": value}


def event_of(db, event_id: int) -> models.Event:
    db.expire_all()
    return db.scalars(select(models.Event).where(models.Event.event_id == event_id)).one()


def alarm_notified(db) -> set[int]:
    db.expire_all()
    rows = db.scalars(select(models.Notification).where(models.Notification.kind == "event.alarm"))
    return {r.payload["event_id"] for r in rows}


# --- чтение и валидация -------------------------------------------------------------

def test_get_returns_verified_values_and_bounds(admin):
    resp = admin.get(URL)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["values"] == data["verified"]
    assert data["version"] == 0 and data["locked"] is False
    assert data["verified"]["gas"] == {"alarm_pct": 1.0, "critical_pct": 5.0}
    assert data["verified"]["limits"] == {"sensor_link": 20, "equipment_diag": 3,
                                          "guard_weekly": 4, "fire_risk": 10, "flood_risk": 5}
    assert data["bounds"]["limits.sensor_link"] == {"min": 1, "max": 20}
    assert data["bounds"]["limits.fire_risk"] == {"min": 1, "max": 10}
    assert data["bounds"]["limits.flood_risk"] == {"min": 1, "max": 5}
    assert data["bounds"]["gas.alarm_pct"] == {"min": 0.1, "max": 5.0}


@pytest.mark.parametrize("changes", [
    {"gas__alarm_pct": 0.05},                       # ниже диапазона
    {"gas__critical_pct": 50},                      # опечатка 50 вместо 5,0
    {"gas__alarm_pct": 5.0, "gas__critical_pct": 5.0},  # тревога не ниже критического
    {"gas_window__hour_from": 15, "gas_window__hour_to": 9},
    {"gas_window__hour_to": 25},
    {"gas_window__days": []},
    {"gas_window__days": [0, 0, 1]},
    {"gas_window__days": [7]},
    {"series__window_min": 0},
    {"series__fire_min": 1},
    {"series__gas_min": 51},
    {"series__work__hour_from": 24},
    {"notify__classes": ["warning"]},
    {"notify__groups": ["smoke"]},
    {"limits__equipment_diag": 0},
])
def test_out_of_range_is_422_and_not_saved(admin, changes):
    resp = put(admin, **changes)
    assert resp.status_code == 422, resp.text
    assert admin.get(URL).json()["version"] == 0


def test_bounds_are_accepted(admin):
    resp = put(admin, gas__alarm_pct=0.1, gas__critical_pct=10.0, series__window_min=60,
               series__fire_min=2, gas_window__hour_from=0, gas_window__hour_to=24,
               gas_window__days=[6, 0], limits__sensor_link=1, notify__classes=[])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version"] == 1 and data["updated_by"] == "admin"
    assert data["values"]["gas_window"]["days"] == [0, 6]


@pytest.mark.parametrize(("scenario", "verified"), sorted(VERIFIED_LIMITS.items()))
def test_limit_above_verified_is_422_with_reason(admin, scenario, verified):
    resp = put(admin, **{f"limits__{scenario}": verified + 1})
    assert resp.status_code == 422
    [error] = resp.json()["detail"]
    assert error["type"] == "limit_above_verified"
    assert error["loc"] == ["body", "values", "limits", scenario]
    assert "не проверялась" in error["msg"] and str(verified) in error["msg"]
    assert put(admin, **{f"limits__{scenario}": verified}).status_code == 200


def test_verified_limits_match_ml_config():
    """Проверенный лимит — бюджет, на котором ML мерил точность; разойтись они не должны."""
    budgets, head = {}, None
    for line in (ROOT / "ml" / "configs" / "heads.yaml").read_text(encoding="utf-8").splitlines():
        if re.match(r"^[A-Za-z_]+:\s*$", line):
            head = line.rstrip(":").strip()
        elif (m := re.match(r"^\s+budget_per_day:\s*(\d+)", line)) and head:
            budgets[head] = int(m.group(1))
    weekly = re.search(r"^BUDGET = (\d+)$", (ROOT / "ml" / "src" / "mkl" / "guard_weekly.py")
                       .read_text(encoding="utf-8"), re.MULTILINE)
    assert VERIFIED_LIMITS == {"sensor_link": budgets["A_link"],
                               "equipment_diag": budgets["D"],
                               "fire_risk": budgets["B"],
                               "flood_risk": budgets["E"],
                               "guard_weekly": int(weekly.group(1))}


def test_saved_before_new_scenarios_keeps_values(admin, db):
    """Параметры, сохранённые до лимитов пожара и подтопления, не сбрасываются целиком:
    новые лимиты берут проверенные значения, остальное — сохранённое."""
    old = values(gas__alarm_pct=0.8, limits__sensor_link=7)
    for key in ("fire_risk", "flood_risk"):
        del old["limits"][key]
    db.add(models.Setting(key=parameters.KEY, value={"value": old, "version": 3,
                                                     "updated_at": None, "updated_by": "admin"}))
    db.commit()
    data = admin.get(URL).json()
    assert data["version"] == 3
    assert data["values"]["gas"]["alarm_pct"] == 0.8
    assert data["values"]["limits"] == {**VERIFIED_LIMITS, "sensor_link": 7}
    assert parameters.current(db).limit_by_head() == {"A_link": 7, "D": 3, "guard_weekly": 4,
                                                      "B": 10, "E": 5}


def test_stale_version_is_409(admin):
    assert put(admin, gas__alarm_pct=0.8).status_code == 200
    resp = put(admin, version=0, gas__alarm_pct=0.9)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "parameters_version_conflict"
    assert admin.get(URL).json()["values"]["gas"]["alarm_pct"] == 0.8


def test_dispatcher_cannot_read_or_change(login):
    dispatcher = login("dispatcher")
    assert dispatcher.get(URL).status_code == 403
    assert put(dispatcher, gas__alarm_pct=0.5).status_code == 403


# --- применение при приёме ----------------------------------------------------------

def test_new_methane_thresholds_change_class_of_new_event(plant, db):
    plant.post(f"{API}/ingest/events", json=[row(1, GAS_CH, "0,7"), row(2, GAS_CH, "2,5")])
    assert [event_of(db, n).event_class for n in (1, 2)] == ["normal", "alarm"]
    assert put(plant, gas__alarm_pct=0.5, gas__critical_pct=2.0).status_code == 200
    plant.post(f"{API}/ingest/events", json=[row(3, GAS_CH, "0,7", time="03:01:00"),
                                             row(4, GAS_CH, "2,5", time="03:01:00")])
    assert [event_of(db, n).event_class for n in (3, 4)] == ["alarm", "critical"]
    # принятые раньше события хранят прежний класс до пересчёта
    assert [event_of(db, n).event_class for n in (1, 2)] == ["normal", "alarm"]


def test_gas_window_hours_and_days_are_configurable(plant, db):
    assert put(plant, gas_window__hour_from=16, gas_window__hour_to=18,
               gas_window__days=[0]).status_code == 200
    plant.post(f"{API}/ingest/events", json=[
        row(1, GAS_CH, "Обнаружен газ", alarm="t", time="16:30:00"),
        row(2, GAS_CH, "Обнаружен газ", alarm="t", time="10:00:00"),
        row(3, GAS_CH, "Обнаружен газ", alarm="t", time="16:30:00", day="2026-06-30")])
    assert event_of(db, 1).hint == "вероятно, ППР или ТО: газ в пн с 16:00 до 17:59"
    assert event_of(db, 2).hint is None      # 10:00 — вне нового окна
    assert event_of(db, 3).hint is None      # вторник — вне дней окна


def test_series_threshold_and_window_are_configurable(admin, db):
    db.add(models.RefObject(id="S-1", level=3, parent_id=None, kind="controlHouse", name="О"))
    for n in range(3):
        db.add(models.RefChannel(id=996000 + n, obj_id="S-1", sensor_type="Тепловой датчик"))
    db.commit()
    rows = [row(n, 996000 + n, "Не замкнут", alarm="t", time=f"10:{7 * n:02d}:00")
            for n in range(3)]                     # три извещателя за 14 минут
    assert put(admin, series__fire_min=3, series__window_min=15).status_code == 200
    admin.post(f"{API}/ingest/events", json=rows)
    hint = "вероятно, ППР или ТО: серия из 3 извещателей за 15 минут"
    assert [event_of(db, n).hint for n in range(3)] == [hint] * 3


def test_disabled_class_and_group_create_no_notification(plant, db, monkeypatch):
    monkeypatch.setattr(notifications.broker, "publish", lambda *a, **k: None)
    assert put(plant, notify__classes=["critical"],
               notify__groups=["fire", "flood", "intrusion", "temperature"]).status_code == 200
    plant.post(f"{API}/ingest/events", json=[
        row(1, UPS_CH, "Питание от батарей", alarm="t"),   # alarm — класс выключен
        row(2, GAS_CH, "Обнаружен газ", alarm="t"),        # critical, группа gas выключена
        row(3, GAS_CH, "6", time="03:05:00"),              # critical без группы — уведомляет
        row(4, HEAT_CH, "Температура ниже 3ºC", alarm="t")])  # critical, temperature
    assert [event_of(db, n).event_class for n in (1, 2, 3, 4)] == ["alarm", "critical",
                                                                        "critical", "critical"]
    assert alarm_notified(db) == {3, 4}


def test_parameters_are_read_once_per_ttl_not_per_event(plant, db, monkeypatch):
    reads = []

    def count(conn, cursor, statement, params, context, executemany):
        if "FROM settings" in statement and "parameters" in str(params):
            reads.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        for batch in range(3):
            plant.post(f"{API}/ingest/events", json=[
                row(10 * batch + n, GAS_CH, "0,2", time=f"04:{batch:02d}:{n:02d}")
                for n in range(5)])
        assert len(reads) == 1                     # 3 пачки по 5 событий — одно чтение
        monkeypatch.setattr(parameters, "CACHE_TTL_S", 0.0)
        plant.post(f"{API}/ingest/events", json=[row(99, GAS_CH, "0,2", time="05:00:00")])
        assert len(reads) == 2                     # кеш истёк — перечитали
    finally:
        event.remove(engine, "before_cursor_execute", count)


# --- лимит в дневном расчёте --------------------------------------------------------

@pytest.fixture
def published(monkeypatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(daily_run, "publish_recorded",
                        lambda row: events.append((row.kind, row.payload)))
    return events


def test_limit_cuts_output_notifications_orders_and_issued_log(admin, db, fake_ml, published):
    def score(request):
        fake_ml.calls.append(("score", request))
        resp = build_score(request)
        # заявка по A_link собирает обе рекомендации головы: вторая за лимитом
        order = next(o for o in resp.work_orders if o.direction == "sensor_failure")
        second = alert_id("A_link", 9000006, request.asof)
        order.alert_ids.append(second)
        order.channels.append(9000006)
        return resp

    fake_ml.score = score
    assert put(admin, limits__sensor_link=1, limits__guard_weekly=1).status_code == 200
    out = admin.post(f"{API}/admin/run-daily", json={"asof": MONDAY.isoformat()}).json()
    assert out["heads"]["A_link"]["alerts_in_budget"] == 1
    assert out["heads"]["guard_weekly"]["alerts_in_budget"] == 1

    shown = {i["id"] for i in admin.get(f"{API}/forecasts", params={"page_size": 100})
             .json()["items"]}
    first, second = (alert_id("A_link", ch, MONDAY) for ch in (9000001, 9000006))
    assert first in shown and second not in shown
    assert len(shown) == 3                         # A_link, D и одна из двух недельных
    db.expire_all()
    cut = db.get(models.Forecast, second)
    assert cut.in_budget is False and cut.extra["cut_by_limit"] is True
    weekly = admin.get(f"{API}/forecasts", params={"scenario": "guard_weekly"}).json()
    assert weekly["total"] == 1

    new_ids = {p["id"] for kind, p in published if kind == "alert.new"}
    assert first in new_ids and second not in new_ids and len(new_ids) == 3

    db.expire_all()
    issued = db.scalars(select(models.IssuedLog.entity_key)
                        .where(models.IssuedLog.head == "A_link")).all()
    assert issued == ["channel:9000001"]
    order = db.scalars(select(models.WorkOrder).where(models.WorkOrder.scenario == "sensor_link")
                       ).one()
    assert order.forecast_ids == [first] and order.channels == [9000001]
    weekly_orders = db.scalar(select(func.count()).select_from(models.WorkOrder)
                              .where(models.WorkOrder.scenario == "guard_weekly"))
    assert weekly_orders == 1

    # пауза в 7 дней — по показанному: срезанный канал завтра снова может быть выдан
    admin.post(f"{API}/admin/run-daily", json={"asof": TUESDAY.isoformat()})
    request = [c[1] for c in fake_ml.calls if c[0] == "score"][-1]
    assert [e.channel for e in request.issued_histories["A_link"]] == [9000001]


# --- пересчёт истории, стенд, аудит -------------------------------------------------

def test_reclassify_applies_new_thresholds_to_history(plant, db):
    plant.post(f"{API}/ingest/events", json=[row(1, GAS_CH, "0,7"), row(2, GAS_CH, "0,3")])
    assert put(plant, gas__alarm_pct=0.5).status_code == 200
    resp = plant.post(f"{API}/admin/reclassify-events",
                      json={"date_from": DAY, "date_to": DAY})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (data["rows"], data["changed"]) == (2, 1)
    assert data["classes"] == [{"old": "normal", "new": "alarm", "count": 1}]
    assert [event_of(db, n).event_class for n in (1, 2)] == ["alarm", "normal"]
    again = plant.post(f"{API}/admin/reclassify-events",
                       json={"date_from": DAY, "date_to": DAY}).json()
    assert again["changed"] == 0


def test_reclassify_period_is_limited(admin):
    resp = admin.post(f"{API}/admin/reclassify-events",
                      json={"date_from": "2026-05-01", "date_to": "2026-06-30"})
    assert resp.status_code == 422
    assert "31" in resp.text


def test_stand_lock_is_403_and_values_read_only(admin, monkeypatch):
    monkeypatch.setenv("DEMO_SETTINGS_LOCKED", "1")
    get_settings.cache_clear()
    resp = put(admin, gas__alarm_pct=0.5)
    assert resp.status_code == 403 and resp.json()["detail"] == "settings_locked"
    resp = admin.post(f"{API}/admin/reclassify-events", json={"date_from": DAY, "date_to": DAY})
    assert resp.status_code == 403 and resp.json()["detail"] == "settings_locked"
    data = admin.get(URL).json()
    assert data["locked"] is True and data["version"] == 0
    assert data["values"]["gas"]["alarm_pct"] == 1.0


def test_change_is_audited_with_what_changed(admin, db):
    assert put(admin, gas__alarm_pct=0.5, limits__equipment_diag=2).status_code == 200
    assert put(admin, version=1, limits__sensor_link=21).status_code == 422
    db.expire_all()
    rows = db.scalars(select(models.AuditRecord).where(models.AuditRecord.path == URL)
                      .order_by(models.AuditRecord.id)).all()
    assert [(r.method, r.status, r.user_login, r.role) for r in rows] == [
        ("PUT", 200, "admin", "admin"), ("PUT", 422, "admin", "admin")]
    assert rows[0].payload == {"version": 1, "changed": {
        "gas.alarm_pct": [1.0, 0.5], "limits.equipment_diag": [3, 2]}}
    items = admin.get(f"{API}/audit").json()["items"]
    saved = next(i for i in items if i["path"] == URL and i["status"] == 200)
    assert saved["payload"]["changed"]["gas.alarm_pct"] == [1.0, 0.5]
