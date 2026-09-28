"""Вход через корпоративный каталог LDAP/AD (ТЗ §11). Живое.

Включается переменной LDAP_URL; при пустой вход идёт только по локальным учётным
записям из users. Пароль проверяет сам каталог: api выполняет simple bind под DN
сотрудника. Пароль не сохраняется в БД и не пишется ни в лог, ни в аудит.

DN сотрудника находится одним из двух способов:
- шаблон LDAP_USER_DN_TEMPLATE: uid={login},ou=people,dc=corp или {login}@corp.local
  (UPN в AD). Bind идёт сразу под ним, затем api читает свою запись сотрудника: по DN
  (право «читать себя» есть почти в любом каталоге) или, для UPN, фильтром
  LDAP_USER_FILTER в LDAP_USER_BASE;
- поиск: bind служебной учётной записью LDAP_BIND_DN (без неё анонимно), фильтр
  LDAP_USER_FILTER в LDAP_USER_BASE, затем bind под найденным DN.

Роль определяется по группам. Группы берутся из атрибута memberOf записи сотрудника (он
есть в AD и в OpenLDAP с оверлеем memberof) и из поиска LDAP_GROUP_FILTER в
LDAP_GROUP_BASE. Какая группа даёт какую роль, задаёт LDAP_ROLE_GROUPS. Если совпало
несколько групп, берётся роль, записанная в LDAP_ROLE_GROUPS раньше.

Сотрудник заводится в users при первом входе, при каждом следующем обновляются имя и
роль. Вместо хеша пароля в строке лежит отпечаток DN (security.DIRECTORY_HASH_PREFIX):
verify_password его не принимает, и локальный вход под этой строкой невозможен. Если
логин из каталога совпал с локальной учётной записью, строка переходит каталогу, и seed
её больше не трогает.

Роль и членство в группах проверяются только при входе. Сотрудник, которого исключили из
группы, работает до конца срока сессии (SESSION_HOURS).

После отказа каталога api OUTAGE_PAUSE_S секунд не обращается к нему и сразу отвечает
Unavailable. Иначе каждый вход под локальной учётной записью ждал бы таймаута: в замере
28.09 с остановленным контейнером каталога (compose.ldap.yaml) вход ждал 10 с, а один
запрос DNS к имени этого контейнера отказывал за 2,6 с. Пауза общая на процесс api.
"""
import hashlib
import json
import logging
import math
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ldap3 import BASE, NONE, SUBTREE, SYNC, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn, parse_dn
from sqlalchemy.orm import Session

from . import models, vocab
from .config import Settings
from .security import DIRECTORY_HASH_PREFIX

log = logging.getLogger(__name__)

# Тесты подменяют стратегию на ldap3.MOCK_SYNC: Server и Connection остаются те же.
CLIENT_STRATEGY = SYNC

SUCCESS = 0
SIZE_LIMIT_EXCEEDED = 4
NO_SUCH_OBJECT = 32
# Коды bind, за которые отвечает каталог или канал, а не пароль: strongerAuthRequired,
# confidentialityRequired (каталог требует TLS), busy, unavailable.
SERVER_SIDE_BIND_CODES = {8, 13, 51, 52}
USER_ATTRS = ["displayName", "cn", "memberOf"]
GROUP_SIZE_LIMIT = 500
LOGIN_MAX = 64   # длина users.login
NAME_MAX = 200   # длина users.name
LOGIN_EXTRA_CHARS = frozenset("._-@")
OUTAGE_PAUSE_S = 30.0


class DirectoryError(Exception):
    """Отказ входа через каталог."""


class BadCredentials(DirectoryError):
    """Нет такого сотрудника или пароль неверен: каталог отвечает на оба случая одним
    кодом 49, и api их тоже не различает."""


class NoRole(DirectoryError):
    """Пароль верный, но ни одна группа сотрудника не указана в LDAP_ROLE_GROUPS."""


class Unavailable(DirectoryError):
    """Каталог не ответил за LDAP_TIMEOUT_S, оборвал соединение, не прошёл TLS или
    отверг служебную учётную запись."""


class Outage:
    """Пауза после отказа каталога (докстрока модуля)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0
        self._reason = ""

    def active(self) -> str | None:
        with self._lock:
            return self._reason if time.monotonic() < self._until else None

    def mark(self, reason: str) -> None:
        with self._lock:
            self._until = time.monotonic() + OUTAGE_PAUSE_S
            self._reason = reason

    def reset(self) -> None:
        with self._lock:
            self._until, self._reason = 0.0, ""


outage = Outage()


@dataclass(frozen=True)
class DirectoryUser:
    login: str
    dn: str
    name: str
    role: str
    groups: tuple[str, ...]


def enabled(settings: Settings) -> bool:
    return bool(settings.ldap_url.strip())


def parse_role_groups(raw: str) -> list[tuple[str, str]]:
    """LDAP_ROLE_GROUPS → [(роль, группа)] в порядке записи.

    Форматы: JSON-объект {"роль": "группа"} или {"роль": ["группа", …]} и строка
    «роль=группа;роль=группа». Группа — CN (mk-dispatchers) или полный DN
    (cn=mk-dispatchers,ou=groups,dc=corp): DN отличается знаком «=». В строковом формате
    роль отделяется от группы первым «=», поэтому DN в нём тоже допустим."""
    raw = raw.strip()
    pairs: list[tuple[str, str]] = []
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"LDAP_ROLE_GROUPS: неверный JSON ({exc.msg})") from None
        for role, groups in data.items():
            items = [groups] if isinstance(groups, str) else groups
            if not isinstance(items, list) or not all(isinstance(g, str) for g in items):
                raise RuntimeError(f"LDAP_ROLE_GROUPS: у роли «{role}» не строка и не список строк")
            pairs.extend((role.strip(), group.strip()) for group in items)
    else:
        for item in raw.split(";"):
            if not item.strip():
                continue
            role, sep, group = item.partition("=")
            if not sep:
                raise RuntimeError(f"LDAP_ROLE_GROUPS: в «{item.strip()}» нет «=»")
            pairs.append((role.strip(), group.strip()))
    allowed = [code for code in vocab.codes("roles") if code != "integration"]
    for role, group in pairs:
        if role not in allowed:
            raise RuntimeError(f"LDAP_ROLE_GROUPS: роль «{role}» не из {', '.join(allowed)}")
        if not group:
            raise RuntimeError(f"LDAP_ROLE_GROUPS: пустая группа у роли «{role}»")
    if not pairs:
        raise RuntimeError("LDAP_ROLE_GROUPS пуст: ни один сотрудник каталога не получит роль")
    return pairs


def check_config(settings: Settings) -> None:
    """Проверка при старте api (create_app): ошибка в настройке каталога останавливает
    процесс с понятным текстом, а не превращает каждый вход в 503."""
    if not enabled(settings):
        return
    parse_role_groups(settings.ldap_role_groups)
    scheme = settings.ldap_url.strip().split("://", 1)[0].lower()
    if scheme not in ("ldap", "ldaps"):
        raise RuntimeError("LDAP_URL: ожидается ldap://хост:порт или ldaps://хост:порт")
    if settings.ldap_starttls and scheme == "ldaps":
        raise RuntimeError("LDAP_STARTTLS=1 вместе с ldaps://: TLS уже есть, оставьте одно")
    template = settings.ldap_user_dn_template
    if template and "{login}" not in template:
        raise RuntimeError("LDAP_USER_DN_TEMPLATE: нет подстановки {login}")
    if not template and not settings.ldap_user_base:
        raise RuntimeError("нужен LDAP_USER_DN_TEMPLATE или LDAP_USER_BASE")
    if template and "=" not in template and not settings.ldap_user_base:
        raise RuntimeError("шаблон вида {login}@домен не DN: для чтения групп нужен LDAP_USER_BASE")
    if settings.ldap_user_base and "{login}" not in settings.ldap_user_filter:
        raise RuntimeError("LDAP_USER_FILTER: нет подстановки {login}")
    ca_file = settings.ldap_tls_ca_file
    if ca_file and not Path(ca_file).is_file():
        raise RuntimeError(f"LDAP_TLS_CA_FILE: файла {ca_file} нет")


def authenticate(login: str, password: str, settings: Settings) -> DirectoryUser:
    """Проверяет пароль каталогом и определяет роль. Исключения: BadCredentials, NoRole,
    Unavailable."""
    # Пустой пароль в simple bind означает анонимный вход, и многие каталоги отвечают на
    # него успехом (RFC 4513, раздел 5.1.2). Логин с посторонними символами в каталог не
    # отправляется: так фильтр и DN не приходится доверять одному экранированию.
    if not password or not _login_allowed(login):
        raise BadCredentials
    mapping = parse_role_groups(settings.ldap_role_groups)
    if (reason := outage.active()) is not None:
        raise Unavailable(f"пауза {OUTAGE_PAUSE_S:g} с после отказа: {reason}")
    try:
        entry, groups = _lookup(login, password, settings)
    except Unavailable as exc:
        outage.mark(str(exc))
        raise
    name = (_values(entry, "displayName") or _values(entry, "cn") or [login])[0]
    role = _role_for(groups, mapping)
    if role is None:
        raise NoRole(f"{len(set(groups))} групп, ни одна не в LDAP_ROLE_GROUPS")
    return DirectoryUser(login=login.lower(), dn=entry["dn"], name=name[:NAME_MAX], role=role,
                         groups=tuple(dict.fromkeys(groups)))


def _lookup(login: str, password: str, settings: Settings) -> tuple[dict, list[str]]:
    """Bind под сотрудником, его запись и DN его групп."""
    server = _server(settings)
    reader = user_conn = None
    try:
        entry = None
        if settings.ldap_user_dn_template:
            bind_dn = settings.ldap_user_dn_template.replace("{login}", escape_rdn(login))
        else:
            reader = _bind(server, settings, settings.ldap_bind_dn or None,
                           settings.ldap_bind_password or None)
            if reader is None:
                raise Unavailable("каталог отверг служебную учётную запись LDAP_BIND_DN")
            entry = _find_user(reader, settings, login)
            if entry is None:
                raise BadCredentials
            bind_dn = entry["dn"]
        user_conn = _bind(server, settings, bind_dn, password)
        if user_conn is None:
            raise BadCredentials
        if entry is None:
            if "=" in bind_dn:
                entry = _read_entry(user_conn, settings, bind_dn)
            else:
                entry = _find_user(user_conn, settings, login)
            entry = entry or {"dn": bind_dn, "attributes": {}}
        groups = _values(entry, "memberOf")
        if settings.ldap_group_base:
            # Служебная учётная запись обычно видит группы, а сотрудник может видеть только
            # свою запись (так настроены права в образе osixia/openldap).
            searcher = reader if reader is not None and settings.ldap_bind_dn else user_conn
            groups += _search_groups(searcher, settings, entry["dn"], login)
    finally:
        _close(reader)
        _close(user_conn)
    return entry, groups


def directory_hash(dn: str) -> str:
    """Значение users.password_hash сотрудника каталога. Оно же даёт отпечаток пароля в
    cookie (security._password_mark): перенос записи в другое подразделение меняет DN и
    гасит старые сессии."""
    return DIRECTORY_HASH_PREFIX + hashlib.sha256(_normalize_dn(dn).encode()).hexdigest()[:32]


def is_directory_row(row: models.User) -> bool:
    return row.password_hash.startswith(DIRECTORY_HASH_PREFIX)


def upsert_user(db: Session, found: DirectoryUser) -> models.User:
    row = db.get(models.User, found.login)
    marker = directory_hash(found.dn)
    if row is None:
        row = models.User(login=found.login, name=found.name, role=found.role,
                          password_hash=marker)
        db.add(row)
    else:
        if not is_directory_row(row):
            log.warning("каталог: локальная учётная запись %s перешла каталогу", found.login)
        row.name, row.role, row.password_hash = found.name, found.role, marker
    db.commit()
    return row


# --- соединение и запросы -----------------------------------------------------------


def _login_allowed(login: str) -> bool:
    return 0 < len(login) <= LOGIN_MAX and all(ch.isalnum() or ch in LOGIN_EXTRA_CHARS
                                               for ch in login)


def _server(settings: Settings) -> Server:
    url = settings.ldap_url.strip()
    tls = None
    if url.lower().startswith("ldaps://") or settings.ldap_starttls:
        # Контекст по умолчанию (ssl.create_default_context) допускает TLS 1.2 и выше;
        # имя хоста в сертификате ldap3 сверяет сам при CERT_REQUIRED.
        tls = Tls(validate=ssl.CERT_REQUIRED if settings.ldap_tls_verify else ssl.CERT_NONE,
                  ca_certs_file=settings.ldap_tls_ca_file or None)
    return Server(url, get_info=NONE, tls=tls, connect_timeout=settings.ldap_timeout_s)


def _bind(server: Server, settings: Settings, user: str | None,
          password: str | None) -> Connection | None:
    """Соединение после успешного bind; None — каталог отверг учётные данные."""
    # Таймаут ответа — целые секунды: вне Windows ldap3 передаёт его в SO_RCVTIMEO через
    # struct.pack("LL", …), и дробное число даёт struct.error вместо соединения.
    conn = Connection(server, user=user, password=password, client_strategy=CLIENT_STRATEGY,
                      receive_timeout=max(1, math.ceil(settings.ldap_timeout_s)),
                      read_only=True, raise_exceptions=False, auto_referrals=False)
    try:
        conn.open(read_server_info=False)
        if settings.ldap_starttls:
            conn.start_tls(read_server_info=False)
        if conn.bind(read_server_info=False):
            return conn
    except (LDAPException, OSError) as exc:
        _close(conn)
        raise Unavailable(f"{type(exc).__name__}: {exc}") from None
    result = conn.result or {}
    _close(conn)
    if result.get("result") in SERVER_SIDE_BIND_CODES:
        raise Unavailable(f"bind: {result.get('description')} {result.get('message', '')}")
    return None


def _close(conn: Connection | None) -> None:
    if conn is None:
        return
    try:
        conn.unbind()
    except (LDAPException, OSError):
        pass


def _search(conn: Connection, settings: Settings, base: str, search_filter: str, scope: str,
            attributes: list[str], size_limit: int, missing_ok: bool = False) -> list[dict]:
    """Записи поиска. noSuchObject при missing_ok — пустой список: так OpenLDAP отвечает и
    на базу, которую учётная запись не вправе читать."""
    try:
        conn.search(base, search_filter, search_scope=scope, attributes=attributes,
                    size_limit=size_limit, time_limit=max(1, int(settings.ldap_timeout_s)))
    except (LDAPException, OSError) as exc:
        raise Unavailable(f"{type(exc).__name__}: {exc}") from None
    result = conn.result or {}
    code = result.get("result")
    if code == NO_SUCH_OBJECT and missing_ok:
        log.warning("каталог: база %s не найдена или не видна учётной записи %s", base, conn.user)
        return []
    if code not in (SUCCESS, SIZE_LIMIT_EXCEEDED):
        raise Unavailable(f"search {base}: {result.get('description')}")
    return [item for item in conn.response or [] if item.get("type") == "searchResEntry"]


def _find_user(conn: Connection, settings: Settings, login: str) -> dict | None:
    search_filter = settings.ldap_user_filter.replace("{login}", escape_filter_chars(login))
    entries = _search(conn, settings, settings.ldap_user_base, search_filter, SUBTREE,
                      USER_ATTRS, size_limit=2)
    if len(entries) > 1:
        log.warning("каталог: LDAP_USER_FILTER нашёл больше одной записи для %s", login)
    return entries[0] if len(entries) == 1 else None


def _read_entry(conn: Connection, settings: Settings, dn: str) -> dict | None:
    entries = _search(conn, settings, dn, "(objectClass=*)", BASE, USER_ATTRS, size_limit=1,
                      missing_ok=True)
    return entries[0] if entries else None


def _search_groups(conn: Connection, settings: Settings, user_dn: str, login: str) -> list[str]:
    search_filter = (settings.ldap_group_filter
                     .replace("{user_dn}", escape_filter_chars(user_dn))
                     .replace("{login}", escape_filter_chars(login)))
    entries = _search(conn, settings, settings.ldap_group_base, search_filter, SUBTREE, ["cn"],
                      size_limit=GROUP_SIZE_LIMIT, missing_ok=True)
    return [item["dn"] for item in entries]


def _values(entry: dict, name: str) -> list[str]:
    for key, value in (entry.get("attributes") or {}).items():
        if key.lower() == name.lower():
            values = value if isinstance(value, list) else [value]
            return [v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
                    for v in values if v not in (None, "", b"")]
    return []


# --- группы и роли --------------------------------------------------------------------


def _normalize_dn(dn: str) -> str:
    try:
        parts = parse_dn(dn, escape=False, strip=True)
    except LDAPException:
        return dn.strip().lower()
    return ",".join(f"{kind.strip().lower()}={value.strip().lower()}" for kind, value, _ in parts)


def _first_value(dn: str) -> str:
    """Значение первого RDN в нижнем регистре: CN группы из её DN."""
    try:
        return parse_dn(dn, escape=False, strip=True)[0][1].strip().lower()
    except LDAPException:
        return dn.strip().lower()


def _role_for(groups: list[str], mapping: list[tuple[str, str]]) -> str | None:
    dns = {_normalize_dn(group) for group in groups}
    cns = {_first_value(group) for group in groups}
    for role, group in mapping:
        if ("=" in group and _normalize_dn(group) in dns) or group.lower() in cns:
            return role
    return None
