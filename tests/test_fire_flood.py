"""Пожарный риск участка (B) и подтопление объекта (E) на настоящей заглушке ML.

FakeMl из conftest этих голов не считает: здесь ML — mkl.product_api в режиме stub в том
же процессе, через HTTP-клиент backend, как в scripts/export_contracts.py. Так проверяется
и C1: ответ заглушки проходит зеркало backend/app/schemas/ml.py.

Канала у прогнозов B и E нет: сущность B — участок объекта (address.segment), E — объект.
"""
import io
from datetime import date, timedelta
from pathlib import Path

import openpyxl
import pytest
from app import models
from app.services.export import COLUMNS
from app.services.ml_client import MlClient
from conftest import MONDAY, TUESDAY
from fastapi.testclient import TestClient
from sqlalchemy import select

API = "/api/v1"
ROOT = Path(__file__).resolve().parents[1]
OBJECT_HEADS = {"B": "fire_risk", "E": "flood_risk"}
DEMO_EVE = date(2026, 6, 29)


class StubMl:
    """MlClient поверх заглушки с журналом вызовов, как у FakeMl."""

    def __init__(self, client: MlClient) -> None:
        self.client = client
        self.calls: list[tuple[str, object]] = []

    def score(self, request):
        self.calls.append(("score", request))
        return self.client.score(request)

    def weekly(self, asof):
        self.calls.append(("weekly", asof))
        return self.client.weekly(asof)

    def ready(self, asof=None):
        return self.client.ready(asof)

    def outcomes(self, items):
        self.calls.append(("outcomes", items))
        return self.client.outcomes(items)


@pytest.fixture
def fake_ml(monkeypatch):
    """Подменяет фикстуру conftest: app, admin и login идут в заглушку ML."""
    monkeypatch.setenv("ML_MODE", "stub")
    monkeypatch.setenv("CONTRACTS_DIR", str(ROOT / "contracts"))
    monkeypatch.syspath_prepend(str(ROOT / "ml" / "src"))
    product_api = pytest.importorskip("mkl.product_api")
    with TestClient(product_api.create_app("stub")) as http:
        yield StubMl(MlClient.from_http(http))


def _run(admin, day) -> dict:
    resp = admin.post(f"{API}/admin/run-daily", json={"asof": day.isoformat()})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rows(db, head: str) -> list[models.Forecast]:
    db.expire_all()
    return list(db.scalars(select(models.Forecast).where(models.Forecast.head == head)))


def test_daily_run_writes_fire_and_flood_without_channel(admin, db, fake_ml):
    out = _run(admin, MONDAY)
    for head, scenario in OBJECT_HEADS.items():
        assert out["heads"][head]["result_status"] == "ok"
        rows = _rows(db, head)
        shown = [r for r in rows if r.in_budget]
        assert shown and all(r.scenario == scenario and r.kind == "alert" for r in rows)
        assert all(r.channel_id is None and r.obj_id for r in rows)
        assert all(r.score_type == "probability" and r.risk is not None for r in shown)
        assert out["heads"][head]["alerts_in_budget"] == len(shown)
    assert len([r for r in _rows(db, "B") if r.in_budget]) <= 10
    assert len([r for r in _rows(db, "E") if r.in_budget]) <= 5
    assert all(r.address["segment"] is not None for r in _rows(db, "B"))
    assert all(r.address.get("segment") is None for r in _rows(db, "E"))

    fire = admin.get(f"{API}/forecasts", params={"scenario": "fire_risk"}).json()["items"]
    assert fire and all(i["channel"] is None and i["segment_label"] for i in fire)
    assert all(i["object"]["name"] for i in fire)
    flood = admin.get(f"{API}/forecasts", params={"scenario": "flood_risk"}).json()["items"]
    assert flood and all(i["channel"] is None and i["segment_label"] is None for i in flood)

    card = admin.get(f"{API}/forecasts/{fire[0]['id']}").json()
    assert card["segment_label"] == fire[0]["segment_label"]
    assert len(card["dynamics_30d"]) == 30 and all(p["events"] == 0 for p in card["dynamics_30d"])
    assert card["coverage_note"].startswith("Оценено ")


def test_issued_log_for_object_heads_is_by_object(admin, db, fake_ml):
    _run(admin, MONDAY)
    for head in OBJECT_HEADS:
        objects = {r.obj_id for r in _rows(db, head) if r.in_budget}
        keys = set(db.scalars(select(models.IssuedLog.entity_key)
                              .where(models.IssuedLog.head == head)))
        assert keys == {f"obj:{obj}" for obj in objects}
    _run(admin, TUESDAY)
    request = [c for kind, c in fake_ml.calls if kind == "score"][-1]
    assert request.heads == ["A_link", "D", "B", "E"]
    for head in OBJECT_HEADS:
        sent = request.issued_histories[head]
        assert sent and all(e.channel is None and e.obj and e.sent_day == MONDAY for e in sent)


def test_outcome_for_fire_goes_with_segment(admin, db, fake_ml):
    _run(admin, MONDAY)
    # Окно B и E за понедельник — сутки вторника: к расчёту среды оно закрыто.
    _run(admin, MONDAY + timedelta(days=2))
    queries = [q for kind, items in fake_ml.calls if kind == "outcomes" for q in items]
    fire = [q for q in queries if q.head == "B"]
    shown = {r.id: r for r in _rows(db, "B") if r.in_budget and r.asof == MONDAY}
    assert {q.id for q in fire} >= set(shown)
    for q in fire:
        assert q.channel is None and q.obj == shown[q.id].obj_id
        assert q.segment is not None and q.segment == shown[q.id].address["segment"]
    flood = [q for q in queries if q.head == "E"]
    assert flood and all(q.channel is None and q.obj and q.segment is None for q in flood)
    db.expire_all()
    facts = {o.forecast_id: o.outcome_auto for o in db.scalars(select(models.Outcome))}
    assert all(facts.get(fid) in {"hit", "miss", "unknown"} for fid in shown)


def test_limits_of_new_scenarios_cut_output(admin, db, fake_ml):
    values = admin.get(f"{API}/settings/parameters").json()
    body = values["values"]
    body["limits"].update(fire_risk=2, flood_risk=1)
    resp = admin.put(f"{API}/settings/parameters",
                     json={"expected_version": values["version"], "values": body})
    assert resp.status_code == 200, resp.text
    out = _run(admin, MONDAY)
    assert out["heads"]["B"]["alerts_in_budget"] == 2
    assert out["heads"]["E"]["alerts_in_budget"] == 1
    shown = admin.get(f"{API}/forecasts", params={"scenario": "fire_risk"}).json()
    assert shown["total"] == 2
    cut = [r for r in _rows(db, "B") if (r.extra or {}).get("cut_by_limit")]
    assert cut and all(not r.in_budget for r in cut)
    assert min(r.rank for r in cut) > max(r.rank for r in _rows(db, "B") if r.in_budget)


@pytest.mark.parametrize(("scenario", "verified"), [("fire_risk", 10), ("flood_risk", 5)])
def test_limit_of_new_scenario_above_verified_is_422(admin, scenario, verified):
    values = admin.get(f"{API}/settings/parameters").json()
    body = values["values"]
    body["limits"][scenario] = verified + 1
    resp = admin.put(f"{API}/settings/parameters",
                     json={"expected_version": values["version"], "values": body})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["type"] == "limit_above_verified"


def test_notification_names_segment_and_orders_are_drafts(admin, db, fake_ml):
    _run(admin, MONDAY)
    db.expire_all()
    alerts = list(db.scalars(select(models.Notification)
                             .where(models.Notification.kind == "alert.new")))
    fire = [n for n in alerts if n.payload.get("scenario") == "fire_risk"]
    assert fire and all("(участок: " in n.title and n.payload["segment_label"] for n in fire)
    assert all(n.payload["channel_id"] is None for n in fire)
    flood = [n for n in alerts if n.payload.get("scenario") == "flood_risk"]
    assert flood and all("участок" not in n.title for n in flood)

    for scenario in OBJECT_HEADS.values():
        orders = admin.get(f"{API}/work-orders", params={"scenario": scenario}).json()["items"]
        assert orders and all(o["status"] == "draft" and o["object"]["id"] for o in orders)
        card = admin.get(f"{API}/work-orders/{orders[0]['id']}").json()
        assert card["channels"] == [] and card["checklist"] == []


def test_dashboard_status_quality_geo_and_export_know_new_scenarios(admin, fake_ml):
    # Открытый прогноз — с окном после полуночи demo_today (30.06): расчёт за 29.06.
    _run(admin, DEMO_EVE)
    summary = admin.get(f"{API}/dashboard/summary").json()
    kpi = {s["scenario"]: s for s in summary["scenarios"]}
    for scenario in OBJECT_HEADS.values():
        assert kpi[scenario]["open_forecasts"] > 0
        assert kpi[scenario]["coverage_fraction"] is not None
    heads = {h["scenario"]: h for h in admin.get(f"{API}/system/status").json()["heads"]}
    assert heads["fire_risk"]["head"] == "B" and heads["fire_risk"]["result_status"] == "ok"
    assert heads["flood_risk"]["head"] == "E" and heads["flood_risk"]["result_status"] == "ok"
    for scenario in OBJECT_HEADS.values():
        quality = admin.get(f"{API}/quality", params={"scenario": scenario})
        assert quality.status_code == 200 and quality.json()["scenario"] == scenario
        weeks = admin.get(f"{API}/forecasts/summary", params={"scenario": scenario}).json()
        assert sum(w["issued"] for w in weeks["weeks"]) > 0

    geo = admin.get(f"{API}/schema.geojson").json()
    objects = {f["properties"]["id"]: f["properties"] for f in geo["features"]
               if f["properties"]["feature"] == "object"}
    fire = admin.get(f"{API}/forecasts", params={"scenario": "fire_risk"}).json()["items"]
    assert all(objects[i["object"]["id"]]["risk_level"] == "high" for i in fire)

    sheet = openpyxl.load_workbook(io.BytesIO(
        admin.get(f"{API}/export/forecasts.xlsx").content)).active
    rows = list(sheet.iter_rows(values_only=True))
    col = {name: n for n, name in enumerate(COLUMNS)}
    fire_rows = [r for r in rows[1:] if r[col["Сценарий"]] == "fire_risk"]
    assert fire_rows and all(r[col["Участок"]] and r[col["Канал"]] is None for r in fire_rows)

    report = admin.get(f"{API}/export/report.pdf")
    assert report.status_code == 200 and report.content.startswith(b"%PDF")
