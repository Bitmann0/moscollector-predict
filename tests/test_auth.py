"""Вход, сессия, права по матрице vocabularies.json, блокировка настроек."""
import pytest
from app import vocab
from app.config import get_settings
from conftest import DEMO_PASSWORD, ROLES
from fastapi.testclient import TestClient

API = "/api/v1"


def test_login_sets_http_only_cookie(app):
    client = TestClient(app)
    resp = client.post(f"{API}/auth/login", json={"login": "dispatcher",
                                                  "password": DEMO_PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "dispatcher"
    assert body["name"] == vocab.title("roles", "dispatcher")
    assert set(body["permissions"]) == vocab.permissions_of("dispatcher")
    cookie = resp.headers["set-cookie"]
    assert "mk_session=" in cookie and "HttpOnly" in cookie
    assert client.get(f"{API}/me").json()["login"] == "dispatcher"


def test_wrong_password_is_401(app):
    resp = TestClient(app).post(f"{API}/auth/login",
                                json={"login": "dispatcher", "password": "wrong-password"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "bad_credentials"


def test_me_without_cookie_is_401(app):
    assert TestClient(app).get(f"{API}/me").status_code == 401


def test_tampered_cookie_is_401(app):
    client = TestClient(app, cookies={"mk_session": "forged.token.value"})
    assert client.get(f"{API}/me").status_code == 401


def test_logout_drops_session(login):
    client = login("analyst")
    assert client.post(f"{API}/auth/logout").status_code == 204
    assert client.get(f"{API}/me").status_code == 401


def test_api_key_gives_integration_role(integration, app):
    assert integration.get(f"{API}/me").json()["role"] == "integration"
    bad = TestClient(app, headers={"X-API-Key": "not-the-key"})
    assert bad.get(f"{API}/me").status_code == 401


def test_put_settings_forbidden_for_dispatcher(login):
    resp = login("dispatcher").put(f"{API}/settings", json={"replay_speed": 120})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "forbidden"


def test_put_settings_locked_for_admin(admin, monkeypatch):
    monkeypatch.setenv("DEMO_SETTINGS_LOCKED", "1")
    get_settings.cache_clear()
    resp = admin.put(f"{API}/settings", json={"replay_speed": 120})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "settings_locked"
    assert admin.get(f"{API}/settings").json()["locked"] is True


def test_put_settings_saves_for_admin(admin):
    resp = admin.put(f"{API}/settings", json={"demo_today": "2026-06-29", "mode": "replay"})
    assert resp.status_code == 200
    assert resp.json() == {"demo_today": "2026-06-29", "mode": "replay", "replay_speed": 60,
                           "locked": False}
    assert admin.get(f"{API}/system/status").json()["demo_today"] == "2026-06-29"


# Эндпоинт → права, любое из которых пускает (как в require_perm роутера).
# Запросы подобраны так, чтобы допущенная роль получала не 403: 200/201/204 или 404.
GUARDED = [
    ("GET", "/forecasts", None, ["view"]),
    ("POST", "/forecasts/missing/decisions",
     {"action": "reject", "reason_code": "false_alarm"}, ["decide"]),
    ("POST", "/forecasts/missing/outcome", {"outcome": "no_event"}, ["outcome"]),
    ("POST", "/work-orders", {"forecast_ids": ["missing"]}, ["work_order_manage"]),
    ("PATCH", "/work-orders/missing", {"expected_status": "draft", "status": "confirmed"},
     ["work_order_manage", "work_order_progress"]),
    ("GET", "/ingest/batches", None, ["ingest"]),
    ("POST", "/ingest/events", [], ["ingest"]),
    ("DELETE", "/ingest/day/2026-06-30", None, ["integration", "admin"]),
    ("GET", "/export/forecasts.xlsx", None, ["export"]),
    ("GET", "/audit", None, ["admin"]),
    ("PUT", "/settings", {"replay_speed": 90}, ["admin"]),
    ("POST", "/reference/sync", None, ["admin"]),
    ("POST", "/admin/run-daily", {"asof": "2026-06-16"}, ["admin"]),
]


@pytest.mark.parametrize("role", [*ROLES, "integration"])
@pytest.mark.parametrize(("method", "path", "body", "perms"), GUARDED,
                         ids=[f"{m} {p}" for m, p, _, _ in GUARDED])
def test_permission_matrix(role, method, path, body, perms, login, integration):
    client = integration if role == "integration" else login(role)
    allowed = any(p in vocab.permissions_of(role) for p in perms)
    kwargs = {} if body is None else {"json": body}
    resp = client.request(method, f"{API}{path}", **kwargs)
    if allowed:
        assert resp.status_code in {200, 201, 204, 404}, resp.text
    else:
        assert resp.status_code == 403, resp.text
