"""Вход, сессии и права (ТЗ §11: RBAC).

Люди входят по логину и паролю, получают подписанную HttpOnly cookie: EventSource
в браузере не умеет ставить заголовки, поэтому токен в заголовке не подходит.
Пароль сверяется с хешем в users или, при заданном LDAP_URL, проверяется каталогом
(backend/app/directory.py); сессия у обоих видов учётных записей одна и та же.
Машинные клиенты (replay.py, эмуляторы, внешние системы) шлют X-API-Key и
получают роль integration. Права роли берутся из матрицы vocabularies.json.

Защита сессии:
- в токене лежит отпечаток хеша пароля: смена DEMO_PASSWORD (и seed) гасит старые сессии;
- в токене лежит случайный jti: выход записывает его в revoked_sessions, и эта cookie,
  в том числе скопированная до выхода, получает 401. Другие сессии того же логина
  живут: под демо-логинами на стенде входят несколько экспертов сразу;
- изменяющий запрос с чужого сайта отклоняется (Sec-Fetch-Site, иначе Origin): с cookie,
  а также вход и выход.
"""
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, vocab
from .config import get_settings
from .db import get_db

COOKIE = "mk_session"
_ITERATIONS = 200_000
# password_hash учётной записи из каталога: пароля в БД нет, его проверяет каталог.
DIRECTORY_HASH_PREFIX = "ldap$"


@dataclass(frozen=True)
class CurrentUser:
    login: str
    name: str
    role: str
    perms: frozenset[str]


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith(DIRECTORY_HASH_PREFIX):
        return False
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex),
                                 int(iterations))
    return hmac.compare_digest(digest.hex(), digest_hex)


def _serializer() -> URLSafeTimedSerializer:
    secret = get_settings().secret_key
    if not secret:
        raise RuntimeError("SECRET_KEY не задан: см. .env.example")
    return URLSafeTimedSerializer(secret, salt="mk-session")


def _password_mark(password_hash: str) -> str:
    """Отпечаток хеша пароля в токене: пароль сменили — старые сессии недействительны."""
    key = get_settings().secret_key.encode()
    return hmac.new(key, password_hash.encode(), hashlib.sha256).hexdigest()[:16]


@dataclass(frozen=True)
class IssuedSession:
    user: models.User
    jti: str
    expires_at: datetime


def issue_session(row: models.User) -> str:
    return _serializer().dumps({"login": row.login, "pw": _password_mark(row.password_hash),
                                "jti": secrets.token_urlsafe(16)})


def read_session(token: str, db: Session) -> IssuedSession:
    """Сессия по cookie. Подпись, возраст и отпечаток пароля должны совпасть, а jti —
    не быть отозванным, иначе 401 session_invalid. Токен без jti (выдан до миграции
    0003) тоже 401: после выката все входят заново один раз."""
    hours = get_settings().session_hours
    try:
        data, signed_at = _serializer().loads(token, max_age=hours * 3600,
                                              return_timestamp=True)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="session_invalid") from None
    jti = data.get("jti")
    if not jti or db.get(models.RevokedSession, jti) is not None:
        raise HTTPException(status_code=401, detail="session_invalid")
    row = db.get(models.User, data.get("login"))
    if row is None or data.get("pw") != _password_mark(row.password_hash):
        raise HTTPException(status_code=401, detail="session_invalid")
    return IssuedSession(row, jti, signed_at + timedelta(hours=hours))


def revoke_session(db: Session, issued: IssuedSession) -> None:
    """Отзывает одну сессию до конца её срока и удаляет записи об истёкших: такие
    токены отклонит проверка возраста, и таблица не растёт. Срок считается по текущему
    SESSION_HOURS. Если его потом увеличить, токен, чья запись уже удалена, снова
    пройдёт до нового срока."""
    db.execute(delete(models.RevokedSession)
               .where(models.RevokedSession.expires_at < datetime.now(UTC)))
    db.add(models.RevokedSession(jti=issued.jti, expires_at=issued.expires_at))
    try:
        db.commit()
    except IntegrityError:  # тот же токен уже отозвал параллельный выход
        db.rollback()


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SAME_SITE_FETCH = {"same-origin", "none"}


def _check_csrf(request: Request) -> None:
    """Cookie браузер шлёт и на запросы, начатые чужой страницей. Изменяющие запросы
    принимаем только со своей страницы. Вызывается из authenticate() и зависимостью
    маршрутов входа и выхода, которые authenticate() не проходят.

    Sec-Fetch-Site браузер ставит только на HTTPS и localhost (спецификация Fetch
    Metadata, раздел 3: только для potentially trustworthy URL), поэтому по голому HTTP
    остаётся Origin. Его браузер добавляет к каждому запросу, кроме GET и HEAD, и своему,
    и чужому (MDN, заголовок Origin). Значение "null" с Host не совпадёт — 403.

    Запрос без обоих заголовков пропускаем. Изменяющий запрос без Origin браузер не
    отправит, а подделать межсайтовый запрос можно только руками браузера жертвы. Без
    этих заголовков ходят скрипты: preload_demo.py, smoke_compose.py (через _api.py,
    urllib) и locust после входа шлют изменяющие запросы с cookie. Требовать заголовки
    от них бессмысленно: клиент вне браузера пришлёт любые."""
    if request.method in SAFE_METHODS:
        return
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None:
        if fetch_site not in SAME_SITE_FETCH:
            raise HTTPException(status_code=403, detail="csrf_rejected")
        return
    origin = request.headers.get("Origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("Host", ""):
        raise HTTPException(status_code=403, detail="csrf_rejected")


def current_user(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    return authenticate(request, db)


def authenticate(request: Request, db: Session) -> CurrentUser:
    """Та же проверка без зависимости FastAPI — для долгих ответов вроде SSE,
    которым нельзя держать сессию БД открытой на всё время соединения."""
    settings = get_settings()
    api_key = request.headers.get("X-API-Key")
    if api_key is not None:
        expected = settings.integration_api_key
        # Сравнение байтов: str с не-ASCII символами compare_digest не принимает (TypeError → 500).
        if expected and hmac.compare_digest(api_key.encode(), expected.encode()):
            user = CurrentUser("integration", "Внешняя система", "integration",
                               vocab.permissions_of("integration"))
            request.state.user = user
            return user
        raise HTTPException(status_code=401, detail="bad_api_key")
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="not_authenticated")
    row = read_session(token, db).user
    _check_csrf(request)
    user = CurrentUser(row.login, row.name, row.role, vocab.permissions_of(row.role))
    request.state.user = user
    return user


def require_perm(*perms: str):
    """Зависимость FastAPI: пропускает, если у роли есть хотя бы одно из прав."""
    def dependency(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if not any(p in user.perms for p in perms):
            raise HTTPException(status_code=403, detail="forbidden")
        return user
    return dependency
