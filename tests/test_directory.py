"""Вход через каталог LDAP (backend/app/directory.py) на подменном каталоге ldap3 MOCK_SYNC.

MOCK_SYNC — стратегия самого ldap3: bind сверяет userPassword записи, поиск разбирает
фильтр, Server и Connection те же, что с настоящим каталогом. Подменяются только
стратегия (directory.CLIENT_STRATEGY) и Server (directory._server): записи каталога живут
в объекте Server, общем для всех соединений теста. Недоступный каталог — настоящие
сокеты: закрытый порт и порт, который принимает соединение и молчит.

Против настоящего OpenLDAP — tests/test_directory_live.py.
"""
import logging
import secrets
import socket
import threading
import time

import pytest
from app import directory, models, vocab
from app.config import get_settings
from app.main import create_app
from app.seed import seed
from app.services.ml_client import get_ml_client
from conftest import DEMO_PASSWORD, ROLES
from fastapi.testclient import TestClient
from ldap3 import MOCK_SYNC, MODIFY_REPLACE, NONE, SYNC, Connection, Server
from sqlalchemy import select

API = "/api/v1"
BASE = "dc=moscollector,dc=test"
PEOPLE = f"ou=people,{BASE}"
GROUPS = f"ou=groups,{BASE}"
SERVICE_DN = f"cn=readonly,{BASE}"
SERVICE_PASSWORD = secrets.token_urlsafe(12)
PASSWORD = secrets.token_urlsafe(12)
ROLE_GROUPS = ";".join(f"{role}=mk-{role}s" for role in ROLES)


def user_dn(login: str) -> str:
    return f"uid={login},{PEOPLE}"


def group_dn(role: str) -> str:
    return f"cn=mk-{role}s,{GROUPS}"


class MockDirectory:
    """Каталог в памяти ldap3: пользователи в ou=people, группы groupOfUniqueNames в ou=groups."""

    def __init__(self) -> None:
        self.server = Server("mock-directory", get_info=NONE)
        self._admin = Connection(self.server, client_strategy=MOCK_SYNC)
        self._admin.bind()
        self._admin.strategy.add_entry(SERVICE_DN, {
            "objectClass": ["organizationalRole", "simpleSecurityObject"], "cn": "readonly",
            "userPassword": SERVICE_PASSWORD})
        self.members: dict[str, list[str]] = {}

    def add_user(self, login: str, groups: list[str], *, member_of: bool = True,
                 name: str | None = None) -> None:
        attrs = {"objectClass": ["inetOrgPerson"], "uid": login, "cn": login, "sn": login,
                 "userPassword": PASSWORD}
        if name:
            attrs["displayName"] = name
        if member_of and groups:
            attrs["memberOf"] = [group_dn(role) for role in groups]
        self._admin.strategy.add_entry(user_dn(login), attrs)
        for role in groups:
            self.members.setdefault(role, []).append(user_dn(login))

    def publish_groups(self) -> None:
        for role, members in self.members.items():
            self._admin.strategy.add_entry(group_dn(role), {
                "objectClass": ["groupOfUniqueNames"], "cn": f"mk-{role}s",
                "uniqueMember": members})

    def set_member_of(self, login: str, roles: list[str]) -> None:
        changes = {"memberOf": [(MODIFY_REPLACE, [group_dn(role) for role in roles])]}
        assert self._admin.modify(user_dn(login), changes), self._admin.result


@pytest.fixture
def mock_dir(monkeypatch) -> MockDirectory:
    mock = MockDirectory()
    for role in ROLES:
        mock.add_user(f"ldap-{role}", [role], name=f"Сотрудник {role}")
    mock.add_user("ldap-norole", [])
    # Группы только в ou=groups, без memberOf: роль даёт поиск групп.
    mock.add_user("ldap-grouponly", ["analyst"], member_of=False)
    mock.add_user("ldap-both", ["technician", "admin"])
    mock.publish_groups()
    monkeypatch.setattr(directory, "CLIENT_STRATEGY", MOCK_SYNC)
    monkeypatch.setattr(directory, "_server", lambda settings: mock.server)
    return mock


LDAP_ENV = {
    "LDAP_URL": "ldap://mock-directory:389",
    "LDAP_BIND_DN": SERVICE_DN,
    "LDAP_BIND_PASSWORD": SERVICE_PASSWORD,
    "LDAP_USER_BASE": PEOPLE,
    "LDAP_USER_FILTER": "(uid={login})",
    "LDAP_GROUP_BASE": GROUPS,
    "LDAP_ROLE_GROUPS": ROLE_GROUPS,
    "LDAP_TIMEOUT_S": "0.5",
    # Остальные переменные каталога сбрасываются: иначе переопределение прошлого
    # configure() в том же тесте осталось бы в окружении.
    "LDAP_USER_DN_TEMPLATE": None,
    "LDAP_STARTTLS": None,
    "LDAP_TLS_CA_FILE": None,
    "LDAP_TLS_VERIFY": None,
    "LDAP_ALLOW_LOCAL": None,
}


@pytest.fixture
def configure(seeded, fake_ml, monkeypatch):
    """configure(**переопределения) → приложение с каталогом; None удаляет переменную."""
    def make(**overrides):
        for key, value in {**LDAP_ENV, **overrides}.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        application = create_app()
        application.dependency_overrides[get_ml_client] = lambda: fake_ml
        return application
    return make


def post_login(application, login: str, password: str = PASSWORD) -> tuple[TestClient, object]:
    client = TestClient(application)
    return client, client.post(f"{API}/auth/login", json={"login": login, "password": password})


# --- роли по группам ----------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_role_comes_from_directory_group(role, mock_dir, configure, db):
    client, resp = post_login(configure(), f"ldap-{role}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["login"] == f"ldap-{role}"
    assert body["role"] == role
    assert body["name"] == f"Сотрудник {role}"
    assert set(body["permissions"]) == vocab.permissions_of(role)
    assert client.get(f"{API}/me").json()["role"] == role
    row = db.get(models.User, f"ldap-{role}")
    assert row.role == role and directory.is_directory_row(row)


def test_group_search_without_member_of(mock_dir, configure):
    _, resp = post_login(configure(), "ldap-grouponly")
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "analyst"


def test_member_of_without_group_search(mock_dir, configure):
    application = configure(LDAP_GROUP_BASE="")
    assert post_login(application, "ldap-dispatcher")[1].json()["role"] == "dispatcher"
    # У этого сотрудника группа только в ou=groups, а поиск групп выключен.
    resp = post_login(application, "ldap-grouponly")[1]
    assert resp.status_code == 403 and resp.json()["detail"] == "no_role_in_directory"


def test_first_matching_role_in_config_order_wins(mock_dir, configure):
    order = "admin=mk-admins;technician=mk-technicians"
    assert post_login(configure(LDAP_ROLE_GROUPS=order), "ldap-both")[1].json()["role"] == "admin"
    order = "technician=mk-technicians;admin=mk-admins"
    resp = post_login(configure(LDAP_ROLE_GROUPS=order), "ldap-both")[1]
    assert resp.json()["role"] == "technician"


def test_role_groups_as_json_with_full_dn(mock_dir, configure):
    mapping = '{"manager": ["cn=mk-managers,ou=groups,dc=moscollector,dc=test"]}'
    _, resp = post_login(configure(LDAP_ROLE_GROUPS=mapping), "ldap-manager")
    assert resp.status_code == 200 and resp.json()["role"] == "manager"


def test_dn_template_mode_without_service_account(mock_dir, configure):
    application = configure(LDAP_USER_DN_TEMPLATE=f"uid={{login}},{PEOPLE}",
                            LDAP_BIND_DN=None, LDAP_BIND_PASSWORD=None, LDAP_USER_BASE=None)
    _, resp = post_login(application, "ldap-analyst")
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "analyst"
    assert post_login(application, "ldap-analyst", "wrong-password")[1].status_code == 401


def test_role_change_in_directory_applies_on_next_login(mock_dir, configure, db):
    application = configure(LDAP_GROUP_BASE="")
    client, resp = post_login(application, "ldap-dispatcher")
    assert resp.json()["role"] == "dispatcher"
    mock_dir.set_member_of("ldap-dispatcher", ["manager"])
    _, resp = post_login(application, "ldap-dispatcher")
    assert resp.json()["role"] == "manager"
    db.expire_all()
    assert db.get(models.User, "ldap-dispatcher").role == "manager"
    # Роль читается из users на каждом запросе: и первая сессия видит новую роль.
    assert client.get(f"{API}/me").json()["role"] == "manager"


def test_login_is_case_insensitive_and_stored_lowercase(mock_dir, configure, db):
    _, resp = post_login(configure(), "LDAP-Admin")
    assert resp.status_code == 200 and resp.json()["login"] == "ldap-admin"
    assert db.scalar(select(models.User.login).where(models.User.login == "ldap-admin"))


# --- отказы -------------------------------------------------------------------------


def test_wrong_password_is_401_and_counted(mock_dir, configure):
    from app.routers.auth import MAX_FAILURES

    application = configure()
    for _ in range(MAX_FAILURES):
        resp = post_login(application, "ldap-admin", "wrong-password")[1]
        assert resp.status_code == 401 and resp.json()["detail"] == "bad_credentials"
    resp = post_login(application, "ldap-admin")[1]
    assert resp.status_code == 429


def test_no_role_group_is_403_and_user_not_created(mock_dir, configure, db):
    _, resp = post_login(configure(), "ldap-norole")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "no_role_in_directory"
    assert db.get(models.User, "ldap-norole") is None


def test_unknown_login_in_directory_falls_back_to_local_account(mock_dir, configure):
    _, resp = post_login(configure(), "dispatcher", DEMO_PASSWORD)
    assert resp.status_code == 200 and resp.json()["role"] == "dispatcher"


def test_local_accounts_off_when_allow_local_is_0(mock_dir, configure):
    application = configure(LDAP_ALLOW_LOCAL="0")
    resp = post_login(application, "dispatcher", DEMO_PASSWORD)[1]
    assert resp.status_code == 401 and resp.json()["detail"] == "bad_credentials"
    assert post_login(application, "ldap-dispatcher")[1].status_code == 200


def test_directory_account_cannot_log_in_locally(mock_dir, configure):
    assert post_login(configure(), "ldap-admin")[1].status_code == 200
    # Каталог выключили: у строки нет хеша пароля, локальный вход её не пускает.
    resp = post_login(configure(LDAP_URL=""), "ldap-admin")[1]
    assert resp.status_code == 401


def test_empty_or_unusual_login_does_not_reach_directory(mock_dir, configure, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("каталог не должен вызываться")

    monkeypatch.setattr(directory, "_bind", fail)
    application = configure()
    for login in ["*", "ldap-admin)(uid=*", "ldap admin", "ldap-admin\\"]:
        resp = post_login(application, login)[1]
        assert resp.status_code == 401, login


def test_directory_takes_over_local_login_and_seed_keeps_it(mock_dir, configure, db):
    mock_dir.add_user("analyst", ["manager"])
    _, resp = post_login(configure(), "analyst")
    assert resp.status_code == 200 and resp.json()["role"] == "manager"
    # Демо-пароль к строке больше не подходит, и перезапуск (seed) её не возвращает.
    assert post_login(configure(), "analyst", DEMO_PASSWORD)[1].status_code == 401
    seed(db)
    db.expire_all()
    assert directory.is_directory_row(db.get(models.User, "analyst"))


# --- недоступный каталог ------------------------------------------------------------


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def unreachable(monkeypatch):
    """Настоящий Server ldap3 на закрытом порту: соединение отклоняется. Linux отвечает
    отказом сразу, Windows повторяет SYN, и отказ приходит по таймауту 0,1 с."""
    port = closed_port()
    monkeypatch.setattr(directory, "_server", lambda settings: Server(
        f"ldap://127.0.0.1:{port}", get_info=NONE, connect_timeout=0.1))
    monkeypatch.setattr(directory, "CLIENT_STRATEGY", SYNC)


def test_unavailable_directory_is_503_and_local_login_works(unreachable, configure):
    application = configure()
    resp = post_login(application, "ldap-admin")[1]
    assert resp.status_code == 503
    assert resp.json()["detail"] == "directory_unavailable"
    resp = post_login(application, "admin", DEMO_PASSWORD)[1]
    assert resp.status_code == 200 and resp.json()["role"] == "admin"
    # Неверный демо-пароль при лежащем каталоге: неизвестно, чей это пароль, — тоже 503.
    assert post_login(application, "admin", "wrong-password")[1].status_code == 503


def test_outage_pause_skips_directory_then_expires(unreachable, configure, monkeypatch):
    application = configure()
    assert post_login(application, "ldap-admin")[1].status_code == 503
    calls = []
    real_bind = directory._bind
    monkeypatch.setattr(directory, "_bind", lambda *a: calls.append(a) or real_bind(*a))
    # Во время паузы каталог не вызывается: и сотрудник, и локальный вход — без ожидания.
    assert post_login(application, "ldap-admin")[1].status_code == 503
    assert post_login(application, "admin", DEMO_PASSWORD)[1].status_code == 200
    assert calls == []
    monkeypatch.setattr(directory, "OUTAGE_PAUSE_S", 0.0)
    directory.outage.mark("тест")
    assert post_login(application, "ldap-admin")[1].status_code == 503
    assert len(calls) == 1


def test_unavailable_directory_without_local_accounts_is_503(unreachable, configure):
    resp = post_login(configure(LDAP_ALLOW_LOCAL="0"), "admin", DEMO_PASSWORD)[1]
    assert resp.status_code == 503 and resp.json()["detail"] == "directory_unavailable"


def test_503_does_not_count_as_failed_attempt(unreachable, configure):
    from app.routers.auth import MAX_FAILURES

    application = configure()
    for _ in range(MAX_FAILURES + 2):
        assert post_login(application, "admin", "wrong-password")[1].status_code == 503
    assert post_login(application, "admin", DEMO_PASSWORD)[1].status_code == 200


@pytest.fixture
def silent_server():
    """TCP-сервер, который принимает соединение и ничего не отвечает."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def serve():
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                accepted.append(listener.accept()[0])
            except TimeoutError:
                continue
            except OSError:
                return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1]
    stop.set()
    thread.join(timeout=2)
    for conn in accepted:
        conn.close()
    listener.close()


def test_silent_directory_times_out_with_503(silent_server, configure, monkeypatch):
    monkeypatch.setattr(directory, "CLIENT_STRATEGY", SYNC)
    application = configure(LDAP_URL=f"ldap://127.0.0.1:{silent_server}", LDAP_TIMEOUT_S="0.3")
    started = time.monotonic()
    resp = post_login(application, "ldap-admin")[1]
    elapsed = time.monotonic() - started
    assert resp.status_code == 503 and resp.json()["detail"] == "directory_unavailable"
    assert elapsed < 5, elapsed


def test_rejected_service_account_is_503(mock_dir, configure):
    resp = post_login(configure(LDAP_BIND_PASSWORD="not-the-service-password"), "ldap-admin")[1]
    assert resp.status_code == 503 and resp.json()["detail"] == "directory_unavailable"


def test_missing_user_base_is_503(mock_dir, configure):
    resp = post_login(configure(LDAP_USER_BASE=f"ou=nowhere,{BASE}"), "ldap-admin")[1]
    assert resp.status_code == 503


# --- пароль не утекает --------------------------------------------------------------


def audit_dump(db) -> str:
    return " ".join(f"{r.user_login} {r.role} {r.path} {r.status} {r.entity} {r.payload}"
                    for r in db.scalars(select(models.AuditRecord)))


def test_password_is_not_logged_or_audited(mock_dir, configure, db, caplog):
    caplog.set_level(logging.DEBUG)
    application = configure()
    wrong = f"wrong-{secrets.token_hex(8)}"
    assert post_login(application, "ldap-admin", wrong)[1].status_code == 401
    assert post_login(application, "ldap-norole")[1].status_code == 403
    assert post_login(application, "ldap-admin")[1].status_code == 200
    assert "ldap-norole" in caplog.text  # отказ по роли в лог пишется
    for secret in (wrong, PASSWORD, SERVICE_PASSWORD):
        assert secret not in caplog.text
        assert secret not in audit_dump(db)


def test_password_is_not_logged_when_directory_is_down(unreachable, configure, db, caplog):
    caplog.set_level(logging.DEBUG)
    secret = f"secret-{secrets.token_hex(8)}"
    assert post_login(configure(), "ldap-admin", secret)[1].status_code == 503
    assert "каталог недоступен при входе ldap-admin" in caplog.text
    assert secret not in caplog.text and SERVICE_PASSWORD not in caplog.text
    assert secret not in audit_dump(db)
    assert "ldap-admin None /api/v1/auth/login 503" in audit_dump(db)


def test_audit_has_directory_login_and_role(mock_dir, configure, db):
    assert post_login(configure(), "ldap-technician")[1].status_code == 200
    assert post_login(configure(), "ldap-technician", "wrong-password")[1].status_code == 401
    rows = db.scalars(select(models.AuditRecord)
                      .where(models.AuditRecord.path == f"{API}/auth/login")
                      .order_by(models.AuditRecord.id)).all()
    assert [(r.user_login, r.role, r.status) for r in rows] == [
        ("ldap-technician", "technician", 200), ("ldap-technician", None, 401)]


# --- настройка ----------------------------------------------------------------------


def test_parse_role_groups_formats(env):
    assert directory.parse_role_groups("dispatcher=mk-d; admin=cn=mk-a,ou=g,dc=x;") == [
        ("dispatcher", "mk-d"), ("admin", "cn=mk-a,ou=g,dc=x")]
    assert directory.parse_role_groups('{"admin": ["a", "b"], "analyst": "c"}') == [
        ("admin", "a"), ("admin", "b"), ("analyst", "c")]


@pytest.mark.parametrize("raw", ["", "integration=mk-int", "boss=mk-boss", "admin=",
                                 "admin", '{"admin": 1}', "{bad json"])
def test_parse_role_groups_rejects(raw, env):
    with pytest.raises(RuntimeError, match="LDAP_ROLE_GROUPS"):
        directory.parse_role_groups(raw)


@pytest.mark.parametrize("overrides", [
    {"LDAP_ROLE_GROUPS": "boss=mk-boss"},
    {"LDAP_USER_BASE": None},
    {"LDAP_URL": "http://ldap:389"},
    {"LDAP_URL": "ldaps://ldap:636", "LDAP_STARTTLS": "1"},
    {"LDAP_USER_DN_TEMPLATE": "{login}@corp.local", "LDAP_USER_BASE": None},
    {"LDAP_USER_FILTER": "(uid=admin)"},
    {"LDAP_TLS_CA_FILE": "/nonexistent/ca.crt"},
], ids=["role", "no-base", "scheme", "starttls-ldaps", "upn-no-base", "filter", "ca-file"])
def test_bad_config_stops_startup(overrides, configure):
    with pytest.raises(RuntimeError):
        configure(**overrides)


def test_empty_ldap_url_keeps_local_only(app):
    resp = post_login(app, "dispatcher", DEMO_PASSWORD)[1]
    assert resp.status_code == 200
