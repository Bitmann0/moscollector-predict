"""Свойства, которые нужны стенду (PM-10): готовность ML на демо-день, частота входа."""
from datetime import date

from app.routers import auth
from conftest import DEMO_PASSWORD
from fastapi.testclient import TestClient

API = "/api/v1"


def test_status_asks_ml_readiness_for_demo_day(admin, fake_ml):
    admin.get(f"{API}/system/status")
    assert ("ready", date(2026, 6, 30)) in fake_ml.calls


def test_login_is_throttled_after_repeated_failures(app):
    client = TestClient(app)
    wrong = {"login": "dispatcher", "password": "не тот"}
    for _ in range(auth.MAX_FAILURES):
        assert client.post(f"{API}/auth/login", json=wrong).status_code == 401
    assert client.post(f"{API}/auth/login", json=wrong).status_code == 429
    right = {"login": "dispatcher", "password": DEMO_PASSWORD}
    assert client.post(f"{API}/auth/login", json=right).status_code == 429
    assert client.post(f"{API}/auth/login",
                       json={"login": "admin", "password": DEMO_PASSWORD}).status_code == 200


def test_successful_login_clears_failures(app):
    client = TestClient(app)
    wrong = {"login": "analyst", "password": "не тот"}
    for _ in range(auth.MAX_FAILURES - 1):
        client.post(f"{API}/auth/login", json=wrong)
    right = {"login": "analyst", "password": DEMO_PASSWORD}
    assert client.post(f"{API}/auth/login", json=right).status_code == 200
    # Счётчик сброшен: снова доступны все MAX_FAILURES попыток, без сброса
    # вторая же неудача дала бы 429.
    for _ in range(auth.MAX_FAILURES):
        assert client.post(f"{API}/auth/login", json=wrong).status_code == 401
