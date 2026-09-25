"""Вход, сессии и права (ТЗ §11: RBAC). Живое; BE-09 расширяет, не меняя интерфейс.

Люди входят по логину и паролю, получают подписанную HttpOnly cookie: EventSource
в браузере не умеет ставить заголовки, поэтому токен в заголовке не подходит.
Машинные клиенты (replay.py, эмуляторы, внешние системы) шлют X-API-Key и
получают роль integration. Права роли берутся из матрицы vocabularies.json.

Защита сессии:
- в токене лежит отпечаток хеша пароля: смена DEMO_PASSWORD (и seed) гасит старые сессии;
- изменяющий запрос по cookie с чужого сайта отклоняется (Sec-Fetch-Site, иначе Origin).
Не сделано, владелец BE-09: отзыв сессии при выходе — нужен users.session_version,
который выход увеличивает, а authenticate() сверяет.
"""
import hashlib
import hmac
import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from . import models, vocab
from .config import get_settings
from .db import get_db

COOKIE = "mk_session"
_ITERATIONS = 200_000


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


def issue_session(login: str, password_hash: str) -> str:
    return _serializer().dumps({"login": login, "pw": _password_mark(password_hash)})


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SAME_SITE_FETCH = {"same-origin", "none"}


def _check_csrf(request: Request) -> None:
    """Cookie браузер шлёт и на запросы, начатые чужой страницей. Изменяющие запросы
    принимаем только со своей страницы: Sec-Fetch-Site есть у всех актуальных браузеров,
    Origin — запасная проверка для остальных."""
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
    try:
        data = _serializer().loads(token, max_age=settings.session_hours * 3600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="session_invalid") from None
    row = db.get(models.User, data.get("login"))
    if row is None or data.get("pw") != _password_mark(row.password_hash):
        raise HTTPException(status_code=401, detail="session_invalid")
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
