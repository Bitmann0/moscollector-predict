"""Аудит (ТЗ §11): изменяющие запросы и выгрузки попадают в audit_log, чтение — нет."""
from app import models
from conftest import DEMO_PASSWORD, TUESDAY, alert_id
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


def _text(row: models.AuditRecord) -> str:
    return " ".join(str(getattr(row, c.name)) for c in models.AuditRecord.__table__.columns)


def test_failed_login_audit_names_tried_login_not_password(seeded, app):
    wrong = "неверный-пароль-7Qx"
    client = TestClient(app)
    client.post(f"{API}/auth/login", json={"login": "admin", "password": wrong})
    client.post(f"{API}/auth/login", json={"login": "no-such-user", "password": wrong})
    client.post(f"{API}/auth/login", json={"login": "admin", "password": DEMO_PASSWORD})
    known, unknown, ok = _rows(seeded)[-3:]
    # Роль у неудачной попытки пустая: вход не состоялся, это логин, а не пользователь.
    assert (known.path, known.status, known.user_login, known.role) == (
        f"{API}/auth/login", 401, "admin", None)
    assert (unknown.status, unknown.user_login, unknown.role) == (401, "no-such-user", None)
    assert (ok.status, ok.user_login, ok.role) == (200, "admin", "admin")
    for row in (known, unknown, ok):
        assert row.payload is None
        assert wrong not in _text(row) and DEMO_PASSWORD not in _text(row)


def test_logout_audit_names_user(seeded, login, app):
    login("analyst").post(f"{API}/auth/logout")
    TestClient(app).post(f"{API}/auth/logout")  # без сессии: выходить некому
    named, anonymous = _rows(seeded)[-2:]
    assert (named.path, named.status, named.user_login, named.role) == (
        f"{API}/auth/logout", 204, "analyst", "analyst")
    assert (anonymous.status, anonymous.user_login) == (204, None)


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
