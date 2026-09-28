"""Отчёт руководству в PDF (ТЗ §8): формат, числа сводки, права, период, аудит.

Текст PDF извлекает pypdf: числа сверяются с /forecasts/summary и /quality, а не с
константами, чтобы расхождение отчёта и экранов было видно сразу.
"""
import io
from datetime import datetime, timedelta

import pytest
from app import models
from app.services.helpers import MSK, now_utc, to_db
from app.services.semantics import GAS_WINDOW_HINT
from conftest import MONDAY, alert_id
from pypdf import PdfReader
from sqlalchemy import select

API = "/api/v1"
URL = f"{API}/export/report.pdf"
SUNDAY = MONDAY + timedelta(days=6)
WEEK = {"from": MONDAY.isoformat(), "to": SUNDAY.isoformat()}


def pdf_text(content: bytes) -> str:
    pages = PdfReader(io.BytesIO(content)).pages
    return "\n".join(page.extract_text() for page in pages).replace("\xa0", " ")


def _outcome(db, forecast_id: str, value: str) -> None:
    row = db.get(models.Outcome, forecast_id)
    if row is None:
        db.add(models.Outcome(forecast_id=forecast_id, outcome_auto=value, updated_at=now_utc()))
    else:
        row.outcome_auto = value


@pytest.fixture
def week(ran, seeded):
    """Понедельник 15.06: одно попадание и один промах у «Отказа датчика», тревоги пожара."""
    db = seeded
    _outcome(db, alert_id("A_link", 9000001, MONDAY), "hit")
    _outcome(db, alert_id("A_link", 9000006, MONDAY), "miss")
    ts = datetime(2026, 6, 16, 10, 0, tzinfo=MSK)
    for n, hint in enumerate([GAS_WINDOW_HINT, None, None]):
        db.add(models.Event(event_id=500 + n, channel_id=9000001, ts=to_db(ts), alarm=True,
                            val_raw="Обнаружен дым", event_class="critical", hint=hint,
                            incident_group="fire", row_hash=f"report-fire-{n}"))
    # 22.06 в 00:30 МСК — по UTC ещё 21.06, последний день периода, но по МСК уже вне его.
    db.add(models.Event(event_id=510, channel_id=9000001,
                        ts=to_db(datetime(2026, 6, 22, 0, 30, tzinfo=MSK)), alarm=True,
                        val_raw="Затоплен", event_class="critical", incident_group="flood",
                        row_hash="report-flood-out"))
    db.commit()
    return db


def test_report_is_pdf_for_default_period(admin, ran):
    resp = admin.get(URL)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    # По умолчанию — семь суток по демо-дату 30.06 (DEMO_TODAY в conftest).
    assert 'filename="report_20260624_20260630.pdf"' in resp.headers["content-disposition"]
    text = pdf_text(resp.content)
    for part in ["АО «Москоллектор»", "Период: 24.06.2026 — 30.06.2026",
                 "демо-дата стенда — 30.06.2026",
                 "Данные демо-стенда: решения за июнь эмулированы",
                 "Сформировано сервисом Москоллектор", "docs/submission/04-ml.md", "Стр. 1 из"]:
        assert part in text, part


def _summed(admin, scenario: str) -> dict:
    weeks = admin.get(f"{API}/forecasts/summary", params={"scenario": scenario, **WEEK}).json()
    return {key: sum(w[key] for w in weeks["weeks"]) for key in ("issued", "hit", "miss",
                                                                  "unknown")}


def test_report_numbers_match_summary_and_quality(admin, week):
    text = pdf_text(admin.get(URL, params=WEEK).content)
    link = _summed(admin, "sensor_link")
    assert (link["hit"], link["miss"]) == (1, 1)
    assert (f"Отказ датчика: риск потери связи {link['issued']} 2 1 1 {link['unknown']} 50 %"
            in text)
    # Та же неделя на экране «Качество» даёт те же числа.
    quality = admin.get(f"{API}/quality", params={"scenario": "sensor_link"}).json()
    same = next(w for w in quality["weeks"] if w["week_start"] == MONDAY.isoformat())
    assert {k: same[k] for k in link} == link

    totals = [_summed(admin, s) for s in ("sensor_link", "equipment_diag", "guard_weekly")]
    issued = sum(t["issued"] for t in totals)
    unknown = sum(t["unknown"] for t in totals)
    assert issued > link["issued"]
    assert f"Все сценарии {issued} 2 1 1 {unknown} 50 %" in text
    # Столбцы диаграммы в сумме дают столько же, сколько строка «Все сценарии».
    assert f"всего {issued} — столько же" in text


def test_report_counts_orders_and_incident_groups(admin, week):
    text = pdf_text(admin.get(URL, params=WEEK).content)
    orders = admin.get(f"{API}/work-orders").json()["items"]
    assert orders and all(o["status"] == "draft" for o in orders)
    assert f"Черновик {len(orders)}" in text
    assert f"Всего {len(orders)}" in text
    # Три тревоги пожара, одна с подсказкой ППР/ТО; наводнение 22.06 вне периода.
    assert "Пожар 3 1 33,3 %" in text
    assert "Наводнение 0 0 —" in text
    assert "Все группы аварий 3 1 33,3 %" in text
    # Канал 9000001 — «Объект-заглушка 1» синтетического справочника: он первый в топе.
    assert "1 Объект-заглушка 1 Комплекс-заглушка 1" in text


def test_report_requires_export_permission(login):
    assert login("technician").get(URL).status_code == 403
    assert login("manager").get(URL).status_code == 200


@pytest.mark.parametrize(("params", "detail"), [
    ({"from": "2026-06-20", "to": "2026-06-10"}, "from_after_to"),
    ({"from": "2025-01-01", "to": "2026-06-30"}, "period_too_long"),
])
def test_report_rejects_bad_period(admin, params, detail):
    resp = admin.get(URL, params=params)
    assert resp.status_code == 422
    assert resp.json()["detail"] == detail


def test_report_download_is_audited(admin, seeded):
    admin.get(URL, params=WEEK)
    seeded.expire_all()
    row = seeded.scalars(select(models.AuditRecord).order_by(models.AuditRecord.id.desc())).first()
    assert (row.method, row.path, row.status, row.user_login) == ("GET", URL, 200, "admin")
