"""Защитные свойства живых частей: предел тела, права, CSRF, сессии, аудит, ML-клиент.

Каждый тест повторяет сценарий подтверждённой находки ревью каркаса.
"""
import importlib
import itertools
import sys
from pathlib import Path

import httpx
import pytest
from app import models
from app.services.ml_client import MlClient, MlUnavailable
from conftest import API_KEY, DEMO_PASSWORD
from fastapi.testclient import TestClient
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
_api = importlib.import_module("_api")

API = "/api/v1"
CROSS_SITE = [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": "http://evil.example"},
]


def test_anonymous_ingest_rejected_before_body(app):
    client = TestClient(app)
    files = {"file": ("journal.csv", b"x" * 1024, "text/csv")}
    resp = client.post(f"{API}/ingest/events/upload", files=files)
    assert resp.status_code == 401


def test_declared_body_over_limit_is_413(login):
    client = login("dispatcher")
    resp = client.post(f"{API}/forecasts/any/decisions", content=b"{}",
                       headers={"Content-Length": str(11 * 1024 * 1024),
                                "Content-Type": "application/json"})
    assert resp.status_code == 413


def test_streamed_body_over_limit_is_413(login):
    client = login("dispatcher")

    def chunks():
        for _ in range(12):
            yield b" " * (1024 * 1024)

    resp = client.post(f"{API}/forecasts/any/decisions", content=chunks(),
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 413


def test_json_batch_over_5000_rows_is_422(integration):
    row = {"ид_события": 1, "ид_канала_данных": 9000001, "дата": "2026-06-30",
           "время": "10:00:00", "тревожное": "f", "значение_датчика": "Норма"}
    resp = integration.post(f"{API}/ingest/events", json=[row] * 5001)
    assert resp.status_code == 422


def test_stream_requires_view_permission(integration):
    assert integration.get(f"{API}/stream").status_code == 403


def test_settings_read_is_admin_only(login):
    assert login("dispatcher").get(f"{API}/settings").status_code == 403
    assert login("admin").get(f"{API}/settings").status_code == 200


def test_non_ascii_api_key_is_401_not_500(app):
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"{API}/forecasts", headers={"X-API-Key": "ключ".encode()})
    assert resp.status_code == 401


def test_valid_api_key_still_works(integration):
    assert integration.get(f"{API}/ingest/batches").status_code == 200
    assert API_KEY


@pytest.mark.parametrize("headers", CROSS_SITE)
def test_cross_site_mutation_with_cookie_is_rejected(login, headers):
    client = login("dispatcher")
    resp = client.post(f"{API}/forecasts/any/decisions",
                       json={"action": "reject", "reason_code": "false_alarm"}, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "csrf_rejected"


def test_same_origin_mutation_passes_csrf(login):
    client = login("dispatcher")
    resp = client.post(f"{API}/forecasts/missing/decisions",
                       json={"action": "reject", "reason_code": "false_alarm"},
                       headers={"Sec-Fetch-Site": "same-origin"})
    assert resp.status_code == 404  # дошли до сервиса: прогноза нет


@pytest.mark.parametrize("headers", CROSS_SITE)
def test_cross_site_login_is_rejected(app, headers):
    client = TestClient(app)
    resp = client.post(f"{API}/auth/login", json={"login": "dispatcher",
                                                  "password": DEMO_PASSWORD}, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "csrf_rejected"
    assert "mk_session" not in client.cookies


@pytest.mark.parametrize("headers", CROSS_SITE)
def test_cross_site_logout_is_rejected_and_keeps_session(login, headers):
    client = login("dispatcher")
    resp = client.post(f"{API}/auth/logout", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "csrf_rejected"
    assert client.get(f"{API}/me").status_code == 200


def test_script_client_without_fetch_headers_logs_in_and_mutates(live_server):
    """scripts/_api.py (urllib) не шлёт ни Sec-Fetch-Site, ни Origin — как preload_demo.py
    и smoke_compose.py: вход и изменяющий запрос по cookie проходят."""
    api = _api.Api(live_server)
    assert api.login("admin", DEMO_PASSWORD)["login"] == "admin"
    status, body = api.call("PUT", "/settings", {"replay_speed": 90})
    assert (status, body["replay_speed"]) == (200, 90)


def test_open_stream_closes_after_logout(live_server):
    with httpx.Client(base_url=live_server, timeout=10) as http:
        token = http.post(f"{API}/auth/login", json={"login": "dispatcher",
                                                     "password": DEMO_PASSWORD}
                          ).cookies["mk_session"]
        headers = {"Cookie": f"mk_session={token}"}
        with http.stream("GET", f"{API}/stream", headers=headers) as resp:
            assert resp.status_code == 200
            lines = resp.iter_lines()
            assert "event: hello" in itertools.islice(lines, 10)
            assert http.post(f"{API}/auth/logout", headers=headers).status_code == 204
            # HEARTBEAT_S = 0.2 (live_server): ping — три строки раз в 0,2 с. Без
            # повторной проверки 60 строк набрались бы за 4 с, и поток шёл бы дальше.
            rest = list(itertools.islice(lines, 60))
    assert len(rest) < 60, rest[-6:]


def test_password_change_invalidates_session(login, db):
    client = login("dispatcher")
    assert client.get(f"{API}/me").status_code == 200
    user = db.get(models.User, "dispatcher")
    user.password_hash = user.password_hash[:-4] + "0000"
    db.commit()
    assert client.get(f"{API}/me").status_code == 401


def test_audit_truncates_long_path(login, db):
    client = login("dispatcher")
    client.patch(f"{API}/work-orders/{'x' * 600}",
                 json={"expected_status": "draft", "status": "confirmed"})
    row = db.scalars(select(models.AuditRecord).order_by(models.AuditRecord.id.desc())).first()
    assert row is not None and len(row.path) <= 500


def test_audit_records_unhandled_500(app, db, monkeypatch):
    from app.services import decisions

    def boom(*args, **kwargs):
        raise RuntimeError("сбой обработчика")

    monkeypatch.setattr(decisions, "create", boom)
    client = TestClient(app, raise_server_exceptions=False)
    from conftest import DEMO_PASSWORD
    client.post(f"{API}/auth/login", json={"login": "dispatcher", "password": DEMO_PASSWORD})
    resp = client.post(f"{API}/forecasts/any/decisions",
                       json={"action": "reject", "reason_code": "false_alarm"})
    assert resp.status_code == 500
    db.expire_all()
    rows = db.scalars(select(models.AuditRecord).where(models.AuditRecord.status == 500)).all()
    assert any(r.path.endswith("/decisions") and r.user_login == "dispatcher" for r in rows)


def test_ml_client_non_json_is_unavailable():
    def handler(request):
        return httpx.Response(200, text="<html>не ML</html>",
                              headers={"Content-Type": "text/html"})

    client = MlClient("http://ml.test", 5.0, transport=httpx.MockTransport(handler))
    with pytest.raises(MlUnavailable):
        client.ready()
