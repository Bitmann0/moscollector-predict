"""Сквозная труба: run_daily с подменённым ML → прогнозы, версии, журнал выданного, заявки."""
import json
from datetime import timedelta
from pathlib import Path

import pytest
from app import models
from app.schemas.ml import ScoreResponse, WeeklyResponse
from app.services import daily_run
from conftest import ALERT_PLAN, MONDAY, TUESDAY, FakeMl, alert_id
from sqlalchemy import func, select

API = "/api/v1"
IN_BUDGET = [p for p in ALERT_PLAN if p[5]]
FIXTURES = Path(__file__).resolve().parents[1] / "contracts" / "fixtures"


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


@pytest.fixture
def published(monkeypatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(daily_run, "publish_safe",
                        lambda kind, payload, **kw: events.append((kind, payload)))
    return events


def test_run_creates_forecasts_and_repeat_adds_only_versions(seeded, fake_ml, published):
    out = daily_run.run_daily(seeded, TUESDAY, fake_ml)
    assert out.forecasts_upserted == len(ALERT_PLAN)
    assert out.work_orders_upserted == 2
    assert out.heads["A_link"].result_status == "ok"
    assert out.heads["A_link"].alerts_in_budget == 2
    assert out.heads["D"].alerts_in_budget == 1
    assert "guard_weekly" not in out.heads  # вторник: недельной очереди нет
    assert [c[0] for c in fake_ml.calls] == ["score"]
    assert _count(seeded, models.ForecastRun) == 1
    assert _count(seeded, models.Forecast) == len(ALERT_PLAN)
    assert _count(seeded, models.ForecastVersion) == len(ALERT_PLAN)
    assert _count(seeded, models.IssuedLog) == len(IN_BUDGET)
    assert [k for k, _ in published].count("alert.new") == len(IN_BUDGET)

    published.clear()
    again = daily_run.run_daily(seeded, TUESDAY, fake_ml)
    assert again.run_id != out.run_id
    assert _count(seeded, models.ForecastRun) == 2
    assert _count(seeded, models.Forecast) == len(ALERT_PLAN)
    assert _count(seeded, models.ForecastVersion) == 2 * len(ALERT_PLAN)
    assert _count(seeded, models.IssuedLog) == len(IN_BUDGET)
    assert _count(seeded, models.WorkOrder) == 2
    assert [k for k, _ in published] == ["run.finished"]  # повтор — без новых alert.new
    row = seeded.get(models.Forecast, alert_id("A_link", 9000001, TUESDAY))
    assert (row.first_run_id, row.last_run_id) == (out.run_id, again.run_id)


def test_forecast_fields_follow_contract(admin, fake_ml):
    admin.post(f"{API}/admin/run-daily", json={"asof": TUESDAY.isoformat()})
    page = admin.get(f"{API}/forecasts").json()
    assert page["total"] == len(IN_BUDGET)  # журнал — только выданное
    first = page["items"][0]
    assert first["id"] == alert_id("A_link", 9000001, TUESDAY)
    assert first["scenario"] == "sensor_link" and first["kind"] == "alert"
    assert first["score_type"] == "probability" and first["source"] == "stub"
    assert first["valid_from"] == "2026-06-17T00:00:00+03:00"
    assert first["valid_to"] == "2026-06-18T00:00:00+03:00"
    assert first["object"]["complex_id"] == "9100"
    assert first["channel"]["id"] == 9000001 and first["channel"]["picket_label"] == "ПК 0"
    assert first["work_order_id"] is not None
    outside = alert_id("A_link", 9000011, TUESDAY)
    assert outside not in {i["id"] for i in page["items"]}
    assert admin.get(f"{API}/forecasts/{outside}").status_code == 200  # карточка — для любой
    d_row = next(i for i in page["items"] if i["head"] == "D")
    assert d_row["scenario"] == "equipment_diag" and d_row["horizon_hours"] == 168
    only_d = admin.get(f"{API}/forecasts", params={"scenario": "equipment_diag"}).json()
    assert only_d["total"] == 1

    card = admin.get(f"{API}/forecasts/{first['id']}").json()
    assert card["factors"][0]["feature"] == "gap_days"
    assert len(card["versions"]) == 1 and len(card["dynamics_30d"]) == 30
    assert card["calendar"] == {"weekday": 2, "weekday_title": "вторник", "holiday": None}
    assert card["coverage_note"].startswith("Оценено 27 из 30")


def test_work_orders_from_ml_are_drafts(admin, fake_ml):
    admin.post(f"{API}/admin/run-daily", json={"asof": TUESDAY.isoformat()})
    orders = admin.get(f"{API}/work-orders").json()["items"]
    by_priority = {o["priority"]: o for o in orders}
    assert set(by_priority) == {"urgent", "planned"}  # «срочная», «плановая» из C1
    assert all(o["status"] == "draft" and o["created_by"] == "system" for o in orders)
    assert by_priority["planned"]["scenario"] == "equipment_diag"
    card = admin.get(f"{API}/work-orders/{by_priority['urgent']['id']}").json()
    assert card["history"][0]["to_status"] == "draft"


def test_issued_history_covers_seven_days_before_asof(seeded, fake_ml, published):
    daily_run.run_daily(seeded, TUESDAY, fake_ml)
    daily_run.run_daily(seeded, TUESDAY + timedelta(days=2), fake_ml)
    request = fake_ml.calls[-1][1]
    assert request.heads == ["A_link", "D"]
    assert request.history_complete_from == TUESDAY + timedelta(days=2) - timedelta(days=7)
    sent = {(e.channel, e.sent_day) for e in request.issued_histories["A_link"]}
    assert sent == {(9000001, TUESDAY), (9000006, TUESDAY)}
    assert [(e.channel, e.sent_day) for e in request.issued_histories["D"]] == [
        (9000016, TUESDAY)]
    keys = set(seeded.scalars(select(models.IssuedLog.entity_key)))
    assert "channel:9000001" in keys

    late = TUESDAY + timedelta(days=8)  # asof − 7 позже дня выдачи: запись вне окна
    daily_run.run_daily(seeded, late, fake_ml)
    old = {e.sent_day for e in fake_ml.calls[-1][1].issued_histories["A_link"]}
    assert TUESDAY not in old


def test_ml_unavailable_gives_error_heads_not_500(admin, fake_ml, seeded):
    fake_ml.fail_score = True
    resp = admin.post(f"{API}/admin/run-daily", json={"asof": TUESDAY.isoformat()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["forecasts_upserted"] == 0
    for head in ["A_link", "D"]:
        assert body["heads"][head]["result_status"] == "error"
        assert "ML недоступен" in body["heads"][head]["detail"]
    run = seeded.scalars(select(models.ForecastRun)).one()
    assert run.heads["A_link"]["result_status"] == "error"
    assert _count(seeded, models.Forecast) == 0
    status = admin.get(f"{API}/system/status").json()
    heads = {h["head"]: h for h in status["heads"]}
    assert heads["A_link"]["result_status"] == "error"
    assert status["last_run"]["run_id"] == body["run_id"]


def test_monday_adds_weekly_queue(admin, ran):
    assert ran["heads"]["guard_weekly"] == {"result_status": "ok", "alerts_in_budget": 2,
                                            "detail": None}
    weekly = admin.get(f"{API}/forecasts", params={"scenario": "guard_weekly"}).json()
    assert weekly["total"] == 2
    item = weekly["items"][0]
    assert item["kind"] == "weekly_recommendation" and item["score_type"] == "relative_priority"
    assert item["risk"] is None and item["priority_score"] == 0.8
    assert item["valid_from"] == "2026-06-17T00:00:00+03:00"  # asof + 2
    assert item["valid_to"] == "2026-06-24T00:00:00+03:00"    # asof + 9, граница не входит
    assert item["horizon_hours"] == 168
    card = admin.get(f"{API}/forecasts/{item['id']}").json()
    assert card["evidence"].startswith("4 дня")
    assert (card["recent_alarm_days_7"], card["recent_alarm_days_30"]) == (4, 11)
    status = {h["head"]: h for h in admin.get(f"{API}/system/status").json()["heads"]}
    assert status["guard_weekly"]["result_status"] == "ok"


def test_weekly_error_is_head_status(seeded, fake_ml, published):
    fake_ml.fail_weekly = True
    out = daily_run.run_daily(seeded, MONDAY, fake_ml)
    assert out.heads["A_link"].result_status == "ok"
    assert out.heads["guard_weekly"].result_status == "error"
    assert "недельной очереди" in out.heads["guard_weekly"].detail


def test_guard_weekly_status_survives_tuesday(admin, ran):
    admin.post(f"{API}/admin/run-daily", json={"asof": TUESDAY.isoformat()})
    status = {h["head"]: h for h in admin.get(f"{API}/system/status").json()["heads"]}
    assert status["guard_weekly"]["result_status"] == "ok"
    assert status["A_link"]["result_status"] == "ok"


def test_decision_appears_in_journal(login, admin, fake_ml):
    admin.post(f"{API}/admin/run-daily", json={"asof": TUESDAY.isoformat()})
    dispatcher = login("dispatcher")
    fid = alert_id("D", 9000016, TUESDAY)
    resp = dispatcher.post(f"{API}/forecasts/{fid}/decisions",
                           json={"action": "reject", "reason_code": "planned_works"})
    assert resp.status_code == 201
    assert resp.json()["author"] == "dispatcher"
    assert resp.json()["created_at"].endswith("+03:00")
    item = next(i for i in dispatcher.get(f"{API}/forecasts").json()["items"] if i["id"] == fid)
    assert item["decision"]["reason_code"] == "planned_works"
    out = dispatcher.post(f"{API}/forecasts/{fid}/outcome",
                          json={"outcome": "no_event", "event_at": "2026-06-18T12:00:00"})
    assert out.status_code == 200
    assert out.json()["event_at"] == "2026-06-18T12:00:00+03:00"
    assert admin.get(f"{API}/forecasts/{fid}").json()["outcome_manual"] == "no_event"
    summary = admin.get(f"{API}/dashboard/summary").json()
    kpi = {s["scenario"]: s for s in summary["scenarios"]}
    assert kpi["sensor_link"]["coverage_fraction"] == 0.9  # охват из последнего прогона


def test_cli_entry_point(seeded, fake_ml, published, monkeypatch, capsys):
    monkeypatch.setattr(daily_run, "get_ml_client", lambda: fake_ml)
    assert daily_run.main(["--asof", "2026-06-16"]) == 0
    assert '"forecasts_upserted": 4' in capsys.readouterr().out
    assert _count(seeded, models.ForecastRun) == 1



class FixtureMl(FakeMl):
    """Ответы ML-заглушки, выгруженные scripts/export_contracts.py в contracts/fixtures."""

    def score(self, request):
        self.calls.append(("score", request))
        return ScoreResponse.model_validate(_fixture("ml_score_2026-06-15.json"))

    def weekly(self, asof):
        self.calls.append(("weekly", asof))
        return WeeklyResponse.model_validate(_fixture("ml_guard_weekly_2026-06-15.json"))


def _fixture(name: str) -> dict:
    path = FIXTURES / name
    if not path.is_file():
        pytest.skip(f"нет {name}: python scripts/export_contracts.py")
    return json.loads(path.read_text(encoding="utf-8"))


def test_run_on_ml_stub_fixture(seeded, published):
    """Тот же прогон на ответе настоящей ML-заглушки: все алерты, заявки и рекомендации легли."""
    score, weekly = _fixture("ml_score_2026-06-15.json"), _fixture("ml_guard_weekly_2026-06-15.json")
    out = daily_run.run_daily(seeded, MONDAY, FixtureMl())
    assert out.forecasts_upserted == len(score["alerts"]) + len(weekly["priorities"])
    assert out.work_orders_upserted == len(score["work_orders"])
    assert _count(seeded, models.IssuedLog) == sum(a["in_budget"] for a in score["alerts"])
    assert {h: s.result_status for h, s in out.heads.items()} == {
        **{h: s["result_status"] for h, s in score["heads"].items()},
        "guard_weekly": weekly["result_status"]}
