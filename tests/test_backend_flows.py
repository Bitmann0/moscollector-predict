"""Сквозные свойства сервисов backend: выгрузка, приём журналов, классы событий, прогон.

Каждый тест повторяет сценарий подтверждённой находки ревью PR #20.
"""
import io
from datetime import date, datetime

import openpyxl
from app import models
from app.schemas.ml import HeadStatus
from app.services import notifications, semantics
from app.services.helpers import MSK, now_utc, to_db
from conftest import build_score
from sqlalchemy import func, select

API = "/api/v1"
GAS = "Газовый датчик"
ROW = {"ид_события": 1, "ид_канала_данных": 9000004, "дата": "2026-06-30",
       "время": "10:00:00", "тревожное": "t", "значение_датчика": "Обнаружен газ"}


def _sheet(resp) -> list[dict]:
    rows = list(openpyxl.load_workbook(io.BytesIO(resp.content)).active.values)
    return [dict(zip(rows[0], row)) for row in rows[1:]]


def test_export_writes_forecast_rows(admin, ran):
    resp = admin.get(f"{API}/export/forecasts.xlsx")
    assert resp.status_code == 200
    rows = _sheet(resp)
    alerts = [row for row in rows if row["Сценарий"] in ("sensor_link", "equipment_diag")]
    assert alerts and all(row["Комплекс"] for row in alerts)
    assert rows[0]["Окно с"].tzinfo is None  # МСК без пояса: Excel поясов не хранит
    orders = {i["id"] for i in admin.get(f"{API}/work-orders").json()["items"]}
    assert any(row["Заявка"] in orders for row in rows)


def test_manual_outcome_correction_keeps_history(admin, ran, db):
    forecast_id = admin.get(f"{API}/forecasts").json()["items"][0]["id"]
    first = admin.post(f"{API}/forecasts/{forecast_id}/outcome",
                       json={"outcome": "confirmed_event", "comment": "первая проверка"})
    second = admin.post(f"{API}/forecasts/{forecast_id}/outcome",
                        json={"outcome": "no_event", "comment": "исправлено"})
    assert first.status_code == second.status_code == 200
    assert second.json()["outcome_manual"] == "no_event"
    history = admin.get(f"{API}/forecasts/{forecast_id}/outcome-history")
    assert history.status_code == 200
    assert [(row["outcome"], row["comment"]) for row in history.json()] == [
        ("no_event", "исправлено"), ("confirmed_event", "первая проверка")]
    assert db.scalar(select(func.count()).select_from(models.ManualOutcomeRevision)) == 2


def test_xlsx_with_native_cells_is_accepted(admin):
    book = openpyxl.Workbook()
    book.active.append(list(ROW))
    book.active.append([2, 9000004, date(2026, 6, 29), "10:00:00", "t", 1.5])
    buf = io.BytesIO()
    book.save(buf)
    resp = admin.post(f"{API}/ingest/events/upload",
                      files={"file": ("e.xlsx", buf.getvalue(), "application/octet-stream")})
    assert (resp.json()["accepted"], resp.json()["rejected"]) == (1, 0)


def test_repeated_row_in_one_batch_is_duplicate(admin):
    resp = admin.post(f"{API}/ingest/events", json=[ROW, ROW])
    assert (resp.json()["accepted"], resp.json()["duplicates"]) == (1, 1)


def test_closed_sse_subscriber_does_not_fail_ingest(admin, monkeypatch, db):
    def closed_loop(*args, **kwargs):
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr(notifications.broker, "publish", closed_loop)
    resp = admin.post(f"{API}/ingest/events", json=[ROW])
    assert resp.status_code == 201
    assert db.scalar(select(func.count()).select_from(models.Notification)) == 1


def _alarm_side_effects(db, published) -> tuple[str | None, int, int]:
    db.expire_all()
    return (db.scalar(select(models.Event.event_class)),
            db.scalar(select(func.count()).select_from(models.Notification)), len(published))


def test_history_batch_with_notify_false_stores_alarm_silently(integration, monkeypatch, db):
    published = []
    monkeypatch.setattr(notifications.broker, "publish", lambda *a, **k: published.append(a))
    resp = integration.post(f"{API}/ingest/events", params={"notify": "false"}, json=[ROW])
    assert resp.json()["accepted"] == 1
    assert _alarm_side_effects(db, published) == ("critical", 0, 0)


def test_live_batch_notifies_by_default(integration, monkeypatch, db):
    published = []
    monkeypatch.setattr(notifications.broker, "publish", lambda *a, **k: published.append(a))
    integration.post(f"{API}/ingest/events", json=[ROW])
    assert _alarm_side_effects(db, published) == ("critical", 1, 1)


def test_history_file_with_notify_false_stores_alarm_silently(admin, monkeypatch, db):
    published = []
    monkeypatch.setattr(notifications.broker, "publish", lambda *a, **k: published.append(a))
    content = ",".join(ROW) + "\n" + ",".join(str(v) for v in ROW.values()) + "\n"
    resp = admin.post(f"{API}/ingest/events/upload", params={"notify": "false"},
                      files={"file": ("e.csv", content.encode(), "text/csv")})
    assert resp.json()["accepted"] == 1
    assert _alarm_side_effects(db, published) == ("critical", 0, 0)


def test_sensor_faults_follow_c5():
    assert semantics.classify(GAS, "-0.4", -0.4, False)[0] == "fault"
    assert semantics.classify(GAS, "327,68", 327.68, True)[0] == "fault"
    assert semantics.classify("Датчик температуры", "999", 999.0, False)[0] == "fault"
    assert semantics.classify(None, "01.01.1970 03:00:05", None, False)[0] == "fault"
    assert semantics.classify(GAS, "2", 2.0, False) == ("alarm", None, None)
    assert semantics.classify(GAS, "6", 6.0, True) == ("critical", None, None)
    # дым без флага тревоги — предупреждение с подсказкой, а не «Норма»
    assert semantics.classify("Датчик дыма", "Обнаружен дым", None, False) == (
        "warning", semantics.NO_ALARM_HINT, None)
    assert semantics.classify("Тепловой датчик", "Температура выше 40ºC", None, False)[0] == "warning"
    assert semantics.classify("Датчик дыма", "Норма", None, False) == ("normal", None, None)


def test_gas_ppr_hint_only_in_weekday_window():
    weekday = datetime(2026, 6, 29, 10, 0, tzinfo=MSK)    # понедельник
    evening = datetime(2026, 6, 29, 15, 0, tzinfo=MSK)
    saturday = datetime(2026, 6, 27, 10, 0, tzinfo=MSK)
    hint = semantics.GAS_WINDOW_HINT
    assert semantics.classify(GAS, "Обнаружен газ", None, True, ts=weekday) == (
        "critical", hint, "gas")
    assert semantics.classify(GAS, "Обнаружен газ", None, True, ts=evening)[1] is None
    assert semantics.classify(GAS, "Обнаружен газ", None, True, ts=saturday)[1] is None


def test_planned_like_kpi_counts_only_planned_hint(admin):
    planned = dict(ROW, время="10:00:00")                       # вторник 30.06, 10:00
    methane = dict(ROW, ид_события=2, время="10:05:00", значение_датчика="2")
    admin.post(f"{API}/ingest/events", json=[planned, methane])
    summary = admin.get(f"{API}/dashboard/summary").json()
    assert (summary["alarms_24h"], summary["planned_like_alarms_24h"]) == (2, 1)


def test_rerun_with_failed_head_keeps_its_issued_log(admin, ran, fake_ml, db):
    def a_link_count() -> int:
        db.expire_all()
        return db.scalar(select(func.count()).select_from(models.IssuedLog)
                         .where(models.IssuedLog.head == "A_link"))

    before = a_link_count()

    def a_link_failed(request):
        resp = build_score(request)
        resp.alerts = [a for a in resp.alerts if a.head != "A_link"]
        resp.heads["A_link"] = HeadStatus(result_status="error", detail="тест")
        return resp

    fake_ml.score = a_link_failed
    assert admin.post(f"{API}/admin/run-daily", json={"asof": "2026-06-15"}).status_code == 200
    assert before > 0 and a_link_count() == before


def test_outcomes_are_not_requested_twice(admin, ran, fake_ml):
    for _ in range(2):  # повтор дня: факт по прогнозам 15.06 уже записан
        admin.post(f"{API}/admin/run-daily", json={"asof": "2026-06-23"})
    sizes = [len(items) for kind, items in fake_ml.calls if kind == "outcomes"]
    assert len(sizes) == 1 and sizes[0] > 0


def test_ods_retry_is_duplicate(integration, db):
    rows = [{"ts": "2026-06-29T10:00:00", "obj_id": "9101", "record_type": "осмотр"}]
    integration.post(f"{API}/ingest/ods-journal", json=rows)
    second = integration.post(f"{API}/ingest/ods-journal", json=rows).json()
    assert (second["accepted"], second["duplicates"]) == (0, 1)
    assert db.scalar(select(func.count()).select_from(models.OdsRecord)) == 1


def test_card_dynamics_uses_moscow_days(admin, ran, db):
    forecast = db.scalars(select(models.Forecast).where(
        models.Forecast.in_budget.is_(True), models.Forecast.channel_id.is_not(None))).first()
    ts = datetime(2026, 6, 15, 1, 30, tzinfo=MSK)  # 22:30 UTC 14.06
    db.add(models.Event(event_id=77, channel_id=forecast.channel_id, ts=to_db(ts), alarm=True,
                        val_raw="Тревога", event_class="alarm", row_hash="dyn-msk"))
    db.commit()
    card = admin.get(f"{API}/forecasts/{forecast.id}").json()
    by_day = {p["day"]: p["alarms"] for p in card["dynamics_30d"]}
    assert (by_day["2026-06-15"], by_day["2026-06-14"]) == (1, 0)


def test_card_dynamics_marks_days_without_events(admin, ran, db):
    """Сутки без событий канала — events=0, а не «ноль тревог» (правило PR #5)."""
    forecast = db.scalars(select(models.Forecast).where(
        models.Forecast.in_budget.is_(True), models.Forecast.channel_id.is_not(None))).first()
    for event_id, ts, alarm, event_class in (
            (81, datetime(2026, 6, 15, 10, 0, tzinfo=MSK), True, "alarm"),
            (82, datetime(2026, 6, 15, 11, 0, tzinfo=MSK), False, "normal"),
            (83, datetime(2026, 6, 13, 9, 0, tzinfo=MSK), False, "normal")):
        db.add(models.Event(event_id=event_id, channel_id=forecast.channel_id, ts=to_db(ts),
                            alarm=alarm, val_raw="x", event_class=event_class,
                            row_hash=f"dyn-gap-{event_id}"))
    db.commit()
    card = admin.get(f"{API}/forecasts/{forecast.id}").json()
    by_day = {p["day"]: (p["events"], p["alarms"]) for p in card["dynamics_30d"]}
    assert len(by_day) == 30
    assert by_day["2026-06-15"] == (2, 1)
    assert by_day["2026-06-14"] == (0, 0)  # нет данных
    assert by_day["2026-06-13"] == (1, 0)  # события были, тревог нет — настоящий ноль

    weekly = db.scalars(select(models.Forecast).where(
        models.Forecast.kind == "weekly_recommendation")).first()
    points = admin.get(f"{API}/forecasts/{weekly.id}").json()["dynamics_30d"]
    assert len(points) == 30 and all(p["events"] == 0 for p in points)


def test_decision_filter_uses_latest_decision(admin, ran):
    fid = admin.get(f"{API}/forecasts").json()["items"][0]["id"]
    for action, reason in (("defer", "await_data"), ("reject", "false_alarm")):
        resp = admin.post(f"{API}/forecasts/{fid}/decisions",
                          json={"action": action, "reason_code": reason})
        assert resp.status_code == 201, resp.text

    def ids(action: str) -> set[str]:
        page = admin.get(f"{API}/forecasts", params={"decision": action}).json()
        return {i["id"] for i in page["items"]}

    assert fid in ids("reject") and fid not in ids("defer")


def test_forecast_summary_counts_whole_filter_by_moscow_week(admin, ran, db):
    """FE-03: итог журнала — по всем строкам фильтра, а не по странице; неделя с понедельника."""
    assert admin.post(f"{API}/admin/run-daily", json={"asof": "2026-06-14"}).status_code == 200
    sunday = admin.get(f"{API}/forecasts", params={"to": "2026-06-14"}).json()["items"]
    for item, value in zip(sunday, ("hit", "miss"), strict=False):
        db.add(models.Outcome(forecast_id=item["id"], outcome_auto=value,
                              updated_at=now_utc(), source="stub"))
    db.commit()
    monday = admin.get(f"{API}/forecasts", params={"from": "2026-06-15"}).json()["items"]
    assert admin.post(f"{API}/forecasts/{monday[0]['id']}/decisions",
                      json={"action": "defer", "reason_code": "await_data"}).status_code == 201

    def weeks(**params) -> dict[str, tuple]:
        resp = admin.get(f"{API}/forecasts/summary", params=params)
        assert resp.status_code == 200, resp.text
        return {w["week_start"]: (w["issued"], w["hit"], w["miss"], w["unknown"], w["decided"])
                for w in resp.json()["weeks"]}

    page = admin.get(f"{API}/forecasts", params={"page_size": 2}).json()
    assert page["total"] == len(sunday) + len(monday) > len(page["items"])
    # Воскресенье 14.06 закрывает неделю с 08.06, понедельник 15.06 открывает следующую.
    assert weeks() == {"2026-06-08": (len(sunday), 1, 1, len(sunday) - 2, 0),
                       "2026-06-15": (len(monday), 0, 0, len(monday), 1)}
    assert weeks(outcome="hit") == {"2026-06-08": (1, 1, 0, 0, 0)}
    assert weeks(decision="any") == {"2026-06-15": (1, 0, 0, 1, 1)}
    grouped = admin.get(f"{API}/forecasts", params={"group_by": "obj"}).json()["total"]
    assert sum(w[0] for w in weeks(group_by="obj").values()) == grouped


def test_coverage_series_skips_days_without_run(admin):
    assert admin.post(f"{API}/admin/run-daily", json={"asof": "2026-06-29"}).status_code == 200
    series = admin.get(f"{API}/dashboard/summary").json()["series_coverage_per_day"]
    assert [p["day"] for p in series] == ["2026-06-29"]
