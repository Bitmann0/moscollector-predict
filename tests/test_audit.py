"""Аудит (ТЗ §11): изменяющие запросы и выгрузки попадают в audit_log, чтение — нет."""
from app import models
from conftest import TUESDAY, alert_id
from fastapi.testclient import TestClient
from sqlalchemy import select

API = "/api/v1"


def _rows(db) -> list[models.AuditRecord]:
    db.expire_all()
    return list(db.scalars(select(models.AuditRecord).order_by(models.AuditRecord.id)))


def test_decision_writes_audit_row(seeded, login, admin):
    admin.post(f"{API}/admin/run-daily", json={"asof": TUESDAY.isoformat()})
    fid = alert_id("A_link", 9000001, TUESDAY)
    before = len(_rows(seeded))
    resp = login("dispatcher").post(f"{API}/forecasts/{fid}/decisions",
                                    json={"action": "remote_check",
                                          "reason_code": "confirmed_by_camera"})
    assert resp.status_code == 201
    rows = _rows(seeded)
    decision = [r for r in rows[before:] if r.path.endswith("/decisions")]
    assert len(decision) == 1
    row = decision[0]
    assert (row.user_login, row.role, row.method, row.status) == ("dispatcher", "dispatcher",
                                                                   "POST", 201)
    assert row.path == f"{API}/forecasts/{fid}/decisions"
    assert row.entity == fid


def test_reads_are_not_audited_but_export_is(seeded, admin):
    before = len(_rows(seeded))
    admin.get(f"{API}/forecasts")
    admin.get(f"{API}/system/status")
    assert len(_rows(seeded)) == before
    admin.get(f"{API}/export/forecasts.xlsx")
    last = _rows(seeded)[-1]
    assert (last.method, last.path, last.user_login) == ("GET", f"{API}/export/forecasts.xlsx",
                                                          "admin")


def test_failed_login_is_audited_without_user(seeded, app):
    TestClient(app).post(f"{API}/auth/login", json={"login": "admin", "password": "nope"})
    last = _rows(seeded)[-1]
    assert (last.path, last.status, last.user_login) == (f"{API}/auth/login", 401, None)


def test_audit_endpoint_lists_and_filters(login, admin):
    login("analyst").post(f"{API}/ingest/events", json=[])
    page = admin.get(f"{API}/audit").json()
    assert page["total"] >= 3  # два входа и приём партии
    assert page["items"][0]["ts"].endswith("+03:00")
    analyst = admin.get(f"{API}/audit", params={"user": "analyst"}).json()
    assert {i["user_login"] for i in analyst["items"]} == {"analyst"}
    assert any(i["path"] == f"{API}/ingest/events" for i in analyst["items"])
    empty = admin.get(f"{API}/audit", params={"from": "2020-01-01", "to": "2020-01-31"}).json()
    assert empty["total"] == 0
