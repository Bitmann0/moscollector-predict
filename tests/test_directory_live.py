"""Вход через настоящий OpenLDAP из compose.ldap.yaml. В CI не запускается: без
LDAP_TEST_URL все тесты пропускаются, а образ osixia/openldap весит 373 МБ.

Запуск вручную из корня репозитория:

    docker compose -f compose.yaml -f compose.ldap.yaml up -d --wait ldap
    docker compose -f compose.yaml -f compose.ldap.yaml cp \\
        ldap:/container/service/slapd/assets/certs/ca.crt state/ldap-ca.crt
    LDAP_TEST_URL=ldap://localhost:1389 LDAP_TEST_TLS_URL=ldaps://localhost:1636 \\
        LDAP_TEST_CA_FILE=state/ldap-ca.crt python -m pytest -q tests/test_directory_live.py

Пароли берутся из deploy/ldap/test-directory.env.example, сотрудники и группы — из
deploy/ldap/bootstrap.ldif. Без LDAP_TEST_TLS_URL и LDAP_TEST_CA_FILE пропускаются
только проверки TLS.
"""
import os
from pathlib import Path

import pytest
from app import directory, models
from app.config import get_settings
from app.main import create_app
from app.services.ml_client import get_ml_client
from conftest import DEMO_PASSWORD, ROLES
from fastapi.testclient import TestClient
from ldap3 import IP_V4_ONLY

pytestmark = [
    pytest.mark.ldap_live,
    pytest.mark.skipif(not os.environ.get("LDAP_TEST_URL"),
                       reason="нет LDAP_TEST_URL: тестовый каталог compose.ldap.yaml не поднят"),
]

API = "/api/v1"
ROOT = Path(__file__).resolve().parents[1]
BASE = "dc=moscollector,dc=test"
URL = os.environ.get("LDAP_TEST_URL", "")
TLS_URL = os.environ.get("LDAP_TEST_TLS_URL", "")
CA_FILE = os.environ.get("LDAP_TEST_CA_FILE", "")
needs_tls = pytest.mark.skipif(not (TLS_URL and CA_FILE),
                               reason="нет LDAP_TEST_TLS_URL или LDAP_TEST_CA_FILE")


def sample_env() -> dict[str, str]:
    path = ROOT / "deploy" / "ldap" / "test-directory.env.example"
    pairs = (line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#"))
    return {key: value for key, value in pairs}


SECRETS = sample_env()


def password(login: str) -> str:
    return SECRETS["LDAP_TEST_PASSWORD_" + login.removeprefix("ldap-").upper()]


# Так же настроен api в compose.ldap.yaml, только каталог — через порт на 127.0.0.1.
SEARCH_MODE = {
    "LDAP_URL": URL,
    "LDAP_BIND_DN": f"cn=readonly,{BASE}",
    "LDAP_BIND_PASSWORD": SECRETS["LDAP_BIND_PASSWORD"],
    "LDAP_USER_BASE": f"ou=people,{BASE}",
    "LDAP_USER_FILTER": "(uid={login})",
    "LDAP_GROUP_BASE": f"ou=groups,{BASE}",
    "LDAP_ROLE_GROUPS": ";".join(f"{role}=mk-{role}s" for role in ROLES),
    "LDAP_TIMEOUT_S": "3",
    # Остальные переменные каталога сбрасываются: иначе переопределение прошлого
    # configure() в том же тесте осталось бы в окружении.
    "LDAP_USER_DN_TEMPLATE": None,
    "LDAP_STARTTLS": None,
    "LDAP_TLS_CA_FILE": None,
    "LDAP_TLS_VERIFY": None,
    "LDAP_ALLOW_LOCAL": None,
}


@pytest.fixture(autouse=True)
def ipv4_only(monkeypatch):
    """В URL стоит имя localhost, а не 127.0.0.1: ldap3 сверяет с сертификатом только
    DNS-имена из subjectAltName, IP-адреса не принимает (ldap3/utils/tls_backport.py).
    Docker публикует порты каталога только на 127.0.0.1, а localhost на Windows сначала
    даёт ::1, где соединение ждёт таймаута. Поэтому имя разрешается только в IPv4."""
    original = directory._server

    def server(settings):
        result = original(settings)
        result.mode = IP_V4_ONLY
        return result

    monkeypatch.setattr(directory, "_server", server)


@pytest.fixture
def configure(seeded, fake_ml, monkeypatch):
    def make(**overrides):
        for key, value in {**SEARCH_MODE, **overrides}.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        application = create_app()
        application.dependency_overrides[get_ml_client] = lambda: fake_ml
        return application
    return make


def post_login(application, login: str, secret: str | None = None):
    client = TestClient(application)
    body = {"login": login, "password": password(login) if secret is None else secret}
    return client, client.post(f"{API}/auth/login", json=body)


@pytest.mark.parametrize("role", ROLES)
def test_each_role_from_real_directory(role, configure, db):
    client, resp = post_login(configure(), f"ldap-{role}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == role and body["login"] == f"ldap-{role}"
    assert body["name"].startswith("Тестовый ")  # displayName из LDIF в base64, UTF-8
    assert client.get(f"{API}/me").json()["role"] == role
    assert directory.is_directory_row(db.get(models.User, f"ldap-{role}"))


def test_wrong_password_no_role_and_local_account(configure):
    application = configure()
    resp = post_login(application, "ldap-admin", "wrong-password")[1]
    assert resp.status_code == 401 and resp.json()["detail"] == "bad_credentials"
    # Пароль сотрудника без роли верный, но mk-visitors роли не даёт.
    resp = post_login(application, "ldap-norole")[1]
    assert resp.status_code == 403 and resp.json()["detail"] == "no_role_in_directory"
    resp = post_login(application, "ldap-ghost", "any-password")[1]
    assert resp.status_code == 401
    resp = post_login(application, "manager", DEMO_PASSWORD)[1]
    assert resp.status_code == 200 and resp.json()["role"] == "manager"


def test_dn_template_mode_reads_own_member_of(configure):
    # Без служебной учётной записи сотрудник видит только свою запись (права в образе
    # osixia), поиск групп возвращает noSuchObject, и роль даёт memberOf.
    application = configure(LDAP_USER_DN_TEMPLATE=f"uid={{login}},ou=people,{BASE}",
                            LDAP_BIND_DN=None, LDAP_BIND_PASSWORD=None, LDAP_USER_BASE=None)
    resp = post_login(application, "ldap-analyst")[1]
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "analyst"
    assert post_login(application, "ldap-norole")[1].status_code == 403
    assert post_login(application, "ldap-analyst", "wrong-password")[1].status_code == 401


def test_role_group_as_full_dn(configure):
    mapping = f"analyst=cn=mk-analysts,ou=groups,{BASE}"
    resp = post_login(configure(LDAP_ROLE_GROUPS=mapping), "ldap-analyst")[1]
    assert resp.status_code == 200 and resp.json()["role"] == "analyst"


def test_group_search_finds_group_by_unique_member(configure):
    # Поиск групп отдельно от memberOf: оверлей memberof заполняет оба пути сразу.
    configure()
    settings = get_settings()
    reader = directory._bind(directory._server(settings), settings, settings.ldap_bind_dn,
                             settings.ldap_bind_password)
    try:
        groups = directory._search_groups(reader, settings, f"uid=ldap-analyst,ou=people,{BASE}",
                                          "ldap-analyst")
    finally:
        directory._close(reader)
    assert groups == [f"cn=mk-analysts,ou=groups,{BASE}"]


@needs_tls
def test_starttls_with_ca(configure):
    resp = post_login(configure(LDAP_STARTTLS="1", LDAP_TLS_CA_FILE=CA_FILE), "ldap-admin")[1]
    assert resp.status_code == 200, resp.text


@needs_tls
def test_ldaps_with_ca(configure):
    resp = post_login(configure(LDAP_URL=TLS_URL, LDAP_TLS_CA_FILE=CA_FILE), "ldap-admin")[1]
    assert resp.status_code == 200, resp.text


@needs_tls
def test_untrusted_certificate_is_503(configure):
    # Тестовый CA образа не в системном хранилище: проверка сертификата не проходит.
    application = configure(LDAP_STARTTLS="1", LDAP_TLS_CA_FILE="")
    resp = post_login(application, "ldap-admin")[1]
    assert resp.status_code == 503 and resp.json()["detail"] == "directory_unavailable"
    # Каждый отказ ставит паузу обращений к каталогу; без сброса следующий вход ответил бы
    # 503, не дойдя до TLS.
    directory.outage.reset()
    resp = post_login(configure(LDAP_URL=TLS_URL, LDAP_TLS_CA_FILE=""), "ldap-admin")[1]
    assert resp.status_code == 503
    directory.outage.reset()
    # С выключенной проверкой тот же сертификат принимается.
    application = configure(LDAP_STARTTLS="1", LDAP_TLS_CA_FILE="", LDAP_TLS_VERIFY="0")
    assert post_login(application, "ldap-admin")[1].status_code == 200
