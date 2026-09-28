"""PM-09: прелоад стенда — недельная очередь без голов A_link и D, черновики недельной
очереди, очистка журнала выданного, эмулированные решения и сам scripts/preload_demo.py."""
import importlib
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from app import models
from app.schemas.ml import OutcomeResult
from app.services import daily_run, emulation
from app.services.helpers import MSK, from_db
from conftest import MONDAY, TUESDAY, FakeMl, alert_id
from sqlalchemy import func, select

API = "/api/v1"
ROOT = Path(__file__).resolve().parents[1]
MATURED = TUESDAY + timedelta(days=2)  # срез 06-19 00:00: окно A_link за 06-16 закрыто
HIT, MISS = alert_id("A_link", 9000001, TUESDAY), alert_id("A_link", 9000006, TUESDAY)
FACT_BY_CHANNEL = {9000001: "hit", 9000006: "miss"}
FACT_BY_OBJ = {"9201": "hit", "9101": "miss"}  # недельная очередь build_weekly
ALL_JUNE = {"date_from": "2026-06-01", "date_to": "2026-06-29", "share": 1}


class FactMl(FakeMl):
    """FakeMl с фактом: hit и miss по каналу или объекту, остальное unknown."""

    def outcomes(self, items):
        self.calls.append(("outcomes", items))
        return [OutcomeResult(id=i.id, outcome=(
            FACT_BY_CHANNEL.get(i.channel, "unknown") if i.channel is not None
            else FACT_BY_OBJ.get(i.obj, "unknown"))) for i in items]


@pytest.fixture
def fake_ml() -> FactMl:
    return FactMl()


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def _run(admin, day: date, **extra) -> dict:
    resp = admin.post(f"{API}/admin/run-daily", json={"asof": day.isoformat(), **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _emulate(admin, **body) -> dict:
    resp = admin.post(f"{API}/admin/emulate-decisions", json={**ALL_JUNE, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _card(client, forecast_id: str) -> dict:
    return client.get(f"{API}/forecasts/{forecast_id}").json()


# --- недельная очередь без голов ---

def test_weekly_only_runs_only_guard_queue(seeded, fake_ml):
    out = daily_run.run_daily(seeded, MONDAY, fake_ml, weekly_only=True)
    assert set(out.heads) == {"guard_weekly"}
    assert [c[0] for c in fake_ml.calls] == ["weekly"]  # /score не вызывался
    assert (out.forecasts_upserted, out.work_orders_upserted) == (2, 2)
    assert _count(seeded, models.IssuedLog) == 0
    assert seeded.scalars(select(models.ForecastRun.kind)).one() == "weekly_guard"
    assert set(seeded.scalars(select(models.Forecast.scenario))) == {"guard_weekly"}
    with pytest.raises(ValueError, match="не понедельник"):
        daily_run.run_daily(seeded, TUESDAY, fake_ml, weekly_only=True)


def test_weekly_only_over_api(admin):
    resp = admin.post(f"{API}/admin/run-daily",
                      json={"asof": TUESDAY.isoformat(), "weekly_only": True})
    assert resp.status_code == 422
    out = _run(admin, MONDAY, weekly_only=True)
    assert set(out["heads"]) == {"guard_weekly"}
    status = admin.get(f"{API}/system/status").json()
    assert status["last_run"]["kind"] == "weekly_guard"


def test_weekly_recommendation_gets_draft_due_at_valid_to(admin, ran):
    weekly = admin.get(f"{API}/forecasts", params={"scenario": "guard_weekly"}).json()["items"]
    orders = admin.get(f"{API}/work-orders",
                       params={"scenario": "guard_weekly"}).json()["items"]
    assert len(weekly) == len(orders) == 2
    by_forecast = {o["forecast_ids"][0]: o for o in orders}
    for item in weekly:
        order = by_forecast[item["id"]]
        assert order["due_by"] == item["valid_to"] == "2026-06-24T00:00:00+03:00"
        assert order["work_type"] == "Проверка охранной сигнализации объекта"
        assert (order["status"], order["priority"], order["created_by"]) == (
            "draft", "planned", "system")
        assert order["object"]["id"] == (item["object"]["id"])
        assert item["work_order_id"] == order["id"]
    card = admin.get(f"{API}/work-orders/{orders[0]['id']}").json()
    assert card["rationale"][0].startswith("охранная тревога в ")
    _run(admin, MONDAY)  # повтор дня обновляет черновики, а не множит
    again = admin.get(f"{API}/work-orders", params={"scenario": "guard_weekly"}).json()
    assert again["total"] == 2


# --- журнал выданного ---

def test_clear_issued_log_deletes_only_range(admin, login, seeded):
    _run(admin, TUESDAY)
    _run(admin, TUESDAY + timedelta(days=1))
    per_day = _count(seeded, models.IssuedLog) // 2
    resp = admin.delete(f"{API}/admin/issued-log",
                        params={"from": TUESDAY.isoformat(), "to": TUESDAY.isoformat()})
    assert resp.status_code == 200
    assert resp.json() == {"date_from": "2026-06-16", "date_to": "2026-06-16",
                           "deleted": per_day}
    assert set(seeded.scalars(select(models.IssuedLog.asof))) == {TUESDAY + timedelta(days=1)}
    assert admin.delete(f"{API}/admin/issued-log",
                        params={"from": "2026-06-17", "to": "2026-06-16"}).status_code == 422
    assert login("dispatcher").delete(f"{API}/admin/issued-log", params={
        "from": "2026-06-01", "to": "2026-06-29"}).status_code == 403


# --- эмулированные решения ---

def _matured(admin) -> None:
    _run(admin, TUESDAY)
    _run(admin, MATURED)


def test_emulated_decisions_follow_fact(admin, seeded):
    _matured(admin)
    out = _emulate(admin)
    assert out == {"with_fact": 2, "decisions": 2, "outcomes": 2, "created": 2, "removed": 0,
                   "skipped_live": 0, "work_orders": 1}
    hit, miss = _card(admin, HIT), _card(admin, MISS)
    assert hit["decision"]["source"] == miss["decision"]["source"] == "emulated"
    assert hit["decision"]["author"] == "emulator"
    assert hit["decision"]["action"] in {"dispatch_crew", "remote_check"}
    assert hit["decision"]["reason_code"] == "confirmed_by_readings"
    assert (hit["outcome_auto"], hit["outcome_manual"]) == ("hit", "confirmed_event")
    assert (miss["decision"]["action"], miss["decision"]["reason_code"]) == (
        "reject", "false_alarm")
    assert (miss["outcome_auto"], miss["outcome_manual"]) == ("miss", "no_event")
    # решение — утром первого дня окна, а не во время прелоада
    assert "2026-06-17T08:00" <= hit["decision"]["created_at"] < "2026-06-17T12:01"
    assert _card(admin, alert_id("D", 9000016, TUESDAY))["decision"] is None  # окно открыто
    assert all(r.action in seeded.get(models.ReasonCode, r.reason_code).actions
               for r in seeded.scalars(select(models.Decision)))
    outcome = seeded.get(models.Outcome, HIT)
    assert (outcome.source, outcome.author) == ("emulated", "emulator")


def _order_of(db, forecast_id: str) -> tuple[str, list[tuple]]:
    db.rollback()
    order = next(o for o in db.scalars(select(models.WorkOrder))
                 if forecast_id in o.forecast_ids)
    history = [(h.from_status, h.to_status, h.author, h.at) for h in db.scalars(
        select(models.WorkOrderHistory).where(models.WorkOrderHistory.order_id == order.id)
        .order_by(models.WorkOrderHistory.id))]
    return order.status, history


def test_emulated_work_orders_follow_decisions(admin, seeded):
    _matured(admin)
    _emulate(admin)
    status, history = _order_of(seeded, HIT)
    assert status == "completed"
    assert [(f, t, a) for f, t, a, _ in history] == [
        (None, "draft", "system"), ("draft", "confirmed", "emulator"),
        ("confirmed", "in_progress", "emulator"), ("in_progress", "completed", "emulator")]
    decided = datetime.fromisoformat(_card(admin, HIT)["decision"]["created_at"])
    # подтверждение — в момент решения, закрытие — к итогу проверки, всё в демо-времени
    assert from_db(history[1][3]) == decided
    assert history[3][3] - history[1][3] == timedelta(hours=emulation.CHECKED_AFTER_H)
    open_order = alert_id("D", 9000016, TUESDAY)  # окно открыто: решения нет
    assert _order_of(seeded, open_order)[0] == "draft"

    again = _emulate(admin)  # повтор даёт то же состояние
    assert again["work_orders"] == 1
    assert [(f, t) for f, t, _, _ in _order_of(seeded, HIT)[1]] == [
        (f, t) for f, t, _, _ in history]
    off = _emulate(admin, share=0)  # без решений заявки возвращаются в черновик
    assert off["work_orders"] == 0
    assert _order_of(seeded, HIT)[0] == "draft"
    assert [(f, t) for f, t, _, _ in _order_of(seeded, HIT)[1]] == [(None, "draft")]


def test_order_steps_cancel_only_when_every_forecast_is_false():
    at = datetime(2026, 6, 17, 9, tzinfo=MSK)
    reject, defer = ("reject", at), ("defer", at)
    assert emulation._order_steps(["a", "b"], {"a": reject, "b": reject}) == [
        ("cancelled", at)]
    assert emulation._order_steps(["a", "b"], {"a": reject}) == []  # у «b» решения нет
    assert emulation._order_steps(["a", "b"], {"a": reject, "b": defer}) == []
    steps = emulation._order_steps(["a", "b"], {"a": reject, "b": ("remote_check", at)})
    assert [s for s, _ in steps] == ["confirmed", "in_progress", "completed"]


def test_emulation_never_moves_orders_touched_by_people(admin, seeded):
    _matured(admin)
    order_id = next(o.id for o in seeded.scalars(select(models.WorkOrder))
                    if HIT in o.forecast_ids)
    resp = admin.patch(f"{API}/work-orders/{order_id}",
                       json={"expected_status": "draft", "status": "cancelled",
                             "reason": "дубль"})
    assert resp.status_code == 200, resp.text
    _emulate(admin)
    status, history = _order_of(seeded, HIT)
    assert status == "cancelled"
    assert [a for _, _, a, _ in history] == ["system", "admin"]


def test_emulation_is_idempotent_and_share_zero_removes_it(admin, seeded):
    _matured(admin)
    _emulate(admin)

    def decisions():
        seeded.rollback()
        return sorted((d.id, d.forecast_id, d.action, d.created_at)
                      for d in seeded.scalars(select(models.Decision)))

    before = decisions()
    again = _emulate(admin)
    assert (again["created"], again["removed"], again["decisions"]) == (0, 0, 2)
    assert decisions() == before  # те же строки с теми же id

    off = _emulate(admin, share=0)
    assert (off["decisions"], off["outcomes"], off["removed"]) == (0, 0, 2)
    hit = _card(admin, HIT)
    assert (hit["decision"], hit["outcome_auto"], hit["outcome_manual"]) == (None, "hit", None)
    assert seeded.get(models.Outcome, HIT).source == "stub"  # происхождение факта вернулось


def test_emulation_never_touches_human_decisions(admin, login, seeded):
    _matured(admin)
    dispatcher = login("dispatcher")
    assert dispatcher.post(f"{API}/forecasts/{HIT}/decisions",
                           json={"action": "defer", "reason_code": "no_crew"}).status_code == 201
    assert dispatcher.post(f"{API}/forecasts/{MISS}/outcome",
                           json={"outcome": "sensor_fault"}).status_code == 200
    out = _emulate(admin)
    assert (out["skipped_live"], out["decisions"], out["created"]) == (2, 0, 0)
    hit, miss = _card(admin, HIT), _card(admin, MISS)
    assert [(d["action"], d["source"]) for d in hit["decisions"]] == [("defer", "live")]
    assert hit["outcome_manual"] is None
    assert miss["decisions"] == [] and miss["outcome_manual"] == "sensor_fault"


def test_human_decision_after_emulation_keeps_both(admin, login):
    _matured(admin)
    _emulate(admin)
    login("dispatcher").post(f"{API}/forecasts/{MISS}/decisions",
                             json={"action": "remote_check", "reason_code": "preventive"})
    out = _emulate(admin, share=0)  # даже при share=0 эмуляция не трогает этот прогноз
    assert (out["skipped_live"], out["removed"]) == (1, 1)
    miss = _card(admin, MISS)
    assert [d["source"] for d in miss["decisions"]] == ["live", "emulated"]
    assert miss["decision"]["source"] == "live" and miss["outcome_manual"] == "no_event"


def test_share_selection_is_deterministic_and_nested():
    ids = [f"forecast-{i}" for i in range(2000)]
    low = {i for i in ids if emulation.selected(i, 0.3)}
    high = {i for i in ids if emulation.selected(i, 0.6)}
    assert low <= high
    assert 0.25 < len(low) / len(ids) < 0.35
    assert low == {i for i in ids if emulation.selected(i, 0.3)}


# --- scripts/preload_demo.py ---

@pytest.fixture
def preload_demo(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("preload_demo")


class ClientApi:
    """Api из scripts/_api.py поверх TestClient: тот же call(), без сети."""

    def __init__(self, client, error_cls) -> None:
        self.client = client
        self.error_cls = error_cls
        self.calls: list[tuple[str, str, dict | None]] = []

    def call(self, method, path, body=None, *, timeout=None):
        self.calls.append((method, path, body))
        resp = self.client.request(method, API + path, json=body)
        if resp.status_code >= 400:
            raise self.error_cls(method, path, resp.status_code, resp.text)
        return resp.status_code, (resp.json() if resp.content else None)


def _state(db) -> dict:
    db.rollback()
    return {
        "issued": sorted((r.head, r.asof, r.entity_key, r.forecast_id)
                         for r in db.scalars(select(models.IssuedLog))),
        "forecasts": sorted(db.scalars(select(models.Forecast.id))),
        "orders": sorted((o.id, o.status, o.due_by) for o in db.scalars(select(models.WorkOrder))),
        "decisions": sorted((d.id, d.forecast_id, d.action, d.reason_code, d.source)
                            for d in db.scalars(select(models.Decision))),
        "outcomes": sorted((o.forecast_id, o.outcome_auto, o.outcome_manual, o.source)
                           for o in db.scalars(select(models.Outcome))),
    }


def test_default_plan_matches_section_3(preload_demo):
    plan = preload_demo.Plan.build(preload_demo.WINDOW_FROM, preload_demo.WINDOW_TO)
    assert (plan.clear_from, plan.clear_to) == (date(2026, 5, 25), date(2026, 6, 29))
    assert (plan.weekly[0], plan.weekly[-1], len(plan.weekly)) == (
        date(2026, 1, 5), date(2026, 5, 25), 21)
    assert (plan.daily[0], plan.daily[-1], len(plan.daily)) == (
        date(2026, 6, 1), date(2026, 6, 29), 29)
    assert (plan.emulate_from, plan.emulate_to) == (date(2026, 1, 5), date(2026, 6, 29))
    later = preload_demo.Plan.build(date(2026, 6, 22), date(2026, 6, 29))
    assert later.clear_from == date(2026, 6, 22)  # неделя до 06-22 — реальная выдача, не трогаем
    assert later.weekly[-1] == date(2026, 6, 15)


def test_preload_calls_in_order_and_rerun_gives_same_state(admin, seeded, fake_ml,
                                                            preload_demo, capsys):
    plan = preload_demo.Plan.build(preload_demo.WINDOW_FROM, preload_demo.WINDOW_TO)
    api = ClientApi(admin, preload_demo.ApiError)
    assert preload_demo.preload(api, plan) == 0
    methods = [(m, p.split("?")[0]) for m, p, _ in api.calls if not p.startswith("/forecasts")
               and not p.startswith("/work-orders")]
    assert methods[0] == ("DELETE", "/admin/issued-log")
    assert methods[-1] == ("POST", "/admin/emulate-decisions")
    runs = [(b["asof"], b.get("weekly_only", False)) for _, p, b in api.calls
            if p == "/admin/run-daily"]
    assert runs == sorted(runs) and len({d for d, _ in runs}) == len(runs)  # строго по возрастанию
    assert [d for d, weekly in runs if weekly] == [d.isoformat() for d in plan.weekly]
    assert [d for d, weekly in runs if not weekly] == [d.isoformat() for d in plan.daily]
    scores = [req for kind, req in fake_ml.calls if kind == "score"]
    assert scores[0].asof == date(2026, 6, 1)
    assert scores[0].history_complete_from == date(2026, 5, 25)
    assert all(r.history_complete_from >= date(2026, 5, 25) for r in scores)

    first = _state(seeded)
    assert any(source == "emulated" for *_, source in first["decisions"])
    assert any(fid.startswith("WO-") for fid, *_ in first["orders"])
    assert "эмуляция 2026-01-05…2026-06-29" in capsys.readouterr().out

    assert preload_demo.preload(ClientApi(admin, preload_demo.ApiError), plan) == 0
    assert _state(seeded) == first


def test_preload_counts_head_errors(admin, fake_ml, preload_demo, capsys):
    fake_ml.fail_score = True
    plan = preload_demo.Plan.build(TUESDAY, TUESDAY, weekly_from=TUESDAY)
    assert plan.weekly == []
    assert preload_demo.preload(ClientApi(admin, preload_demo.ApiError), plan) == 1
    assert "ОШИБКА A_link, D" in capsys.readouterr().out


def test_dry_run_needs_no_password(preload_demo, monkeypatch, capsys):
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    monkeypatch.setattr("sys.argv", ["preload_demo.py", "--dry-run"])
    assert preload_demo.main() == 0
    assert "понедельников до окна: 21" in capsys.readouterr().out
