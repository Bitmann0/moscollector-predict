"""Вход, выход и текущий пользователь. Живое.

Вход и выход не проходят authenticate(), поэтому проверку межсайтового запроса
(security._check_csrf) получают зависимостью маршрута. Вход с чужой страницы входит
жертвой под чужой учётной записью. Выход с чужой страницы завершает сессию жертвы:
cookie SameSite=Lax браузер шлёт и на запрос со страницы соседнего поддомена
(same-site).

Частота входа ограничена здесь, а не в Caddy: в сборке caddy:2-alpine нет модуля
rate limit. Счётчик неудачных попыток живёт в процессе api (на стенде один процесс)
и сбрасывается при перезапуске — для перебора паролей этого достаточно. Для каталога
счётчик ещё и бережёт учётные записи сотрудников: AD блокирует запись после N неверных
паролей подряд, а 429 до каталога не доходит.

Порядок проверки пароля при заданном LDAP_URL (backend/app/directory.py):
1. каталог. Пароль верный и группа даёт роль — вход; верный без роли — 403
   no_role_in_directory, без попытки локального входа;
2. локальная учётная запись из users, если LDAP_ALLOW_LOCAL=1 (по умолчанию). Сюда
   доходят и неверный пароль каталога, и отказ каталога: демо-учётки работают без него;
3. ничего не подошло: 503 directory_unavailable, если каталог не ответил, иначе 401
   bad_credentials. 503 не считается неудачной попыткой.
"""
import logging
import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .. import directory, models, vocab
from ..config import get_settings
from ..db import get_db
from ..schemas.auth import LoginIn, UserOut
from ..security import (
    COOKIE,
    CurrentUser,
    _check_csrf,
    current_user,
    issue_session,
    read_session,
    revoke_session,
    verify_password,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

MAX_FAILURES = 10    # неудачных входов с одного адреса на один логин
WINDOW_S = 300       # за 5 минут; дальше 429 до конца окна


class LoginThrottle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def _recent(self, key: tuple[str, str], now: float) -> deque[float]:
        attempts = self._failures[key]
        while attempts and now - attempts[0] > WINDOW_S:
            attempts.popleft()
        return attempts

    def blocked(self, key: tuple[str, str]) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) >= MAX_FAILURES

    def failed(self, key: tuple[str, str]) -> None:
        with self._lock:
            now = time.monotonic()
            self._recent(key, now).append(now)

    def succeeded(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


throttle = LoginThrottle()


def _out(user: CurrentUser) -> UserOut:
    return UserOut(login=user.login, name=user.name, role=user.role,
                   permissions=sorted(user.perms))


def _user(row: models.User) -> CurrentUser:
    return CurrentUser(row.login, row.name, row.role, vocab.permissions_of(row.role))


LOGIN_RESPONSES = {
    401: {"description": "Неверный логин или пароль: `bad_credentials`"},
    403: {"description": "`no_role_in_directory` — пароль принят каталогом, но ни одна группа "
                         "сотрудника не сопоставлена роли (LDAP_ROLE_GROUPS); "
                         "`csrf_rejected` — вход со страницы другого сайта"},
    429: {"description": "`too_many_attempts` — 10 неудачных входов за 5 минут "
                         "с одного адреса на один логин"},
    503: {"description": "`directory_unavailable` — каталог LDAP не ответил, а локальная "
                         "учётная запись не подошла или выключена (LDAP_ALLOW_LOCAL=0)"},
}


def _directory_user(body: LoginIn, db: Session) -> tuple[models.User | None, bool]:
    """(строка users, каталог_недоступен). Строка есть, только если каталог принял пароль."""
    settings = get_settings()
    try:
        found = directory.authenticate(body.login, body.password, settings)
    except directory.BadCredentials:
        return None, False
    except directory.Unavailable as exc:
        log.warning("каталог недоступен при входе %s: %s", body.login, exc)
        return None, True
    except directory.NoRole as exc:
        log.warning("каталог: у %s нет роли (%s)", body.login, exc)
        raise HTTPException(status_code=403, detail="no_role_in_directory") from None
    return directory.upsert_user(db, found), False


def _local_user(body: LoginIn, db: Session) -> models.User | None:
    row = db.get(models.User, body.login)
    if row is None or not verify_password(body.password, row.password_hash):
        return None
    return row


@router.post("/auth/login", response_model=UserOut, dependencies=[Depends(_check_csrf)],
             responses=LOGIN_RESPONSES)
def login(body: LoginIn, request: Request, response: Response,
          db: Session = Depends(get_db)) -> UserOut:
    # Аудит неудачного входа: какую учётную запись пробовали. Пароль в state не кладём.
    request.state.login_attempt = body.login
    # За Caddy адрес клиента берётся из X-Forwarded-For (uvicorn --proxy-headers).
    key = (request.client.host if request.client else "", body.login)
    if throttle.blocked(key):
        raise HTTPException(status_code=429, detail="too_many_attempts")
    settings = get_settings()
    row, directory_down = None, False
    if directory.enabled(settings):
        row, directory_down = _directory_user(body, db)
    if row is None and (not directory.enabled(settings) or settings.ldap_allow_local):
        row = _local_user(body, db)
    if row is None:
        if directory_down:
            raise HTTPException(status_code=503, detail="directory_unavailable")
        throttle.failed(key)
        raise HTTPException(status_code=401, detail="bad_credentials")
    throttle.succeeded(key)
    response.set_cookie(COOKIE, issue_session(row), httponly=True, samesite="lax",
                        secure=settings.cookie_secure, max_age=settings.session_hours * 3600)
    user = _user(row)
    request.state.user = user
    return _out(user)


@router.post("/auth/logout", status_code=204, dependencies=[Depends(_check_csrf)])
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    """Завершает эту сессию на сервере и удаляет cookie. Другие сессии того же логина
    остаются. Без действующей сессии тоже 204."""
    # Сессию читаем до очистки cookie: без неё не узнать, какой jti отзывать и кого
    # писать в аудит. Недействительную сессию отзывать не нужно.
    token = request.cookies.get(COOKIE)
    issued = None
    if token:
        try:
            issued = read_session(token, db)
        except HTTPException:
            pass
    if issued is not None:
        request.state.user = _user(issued.user)
        revoke_session(db, issued)
    response = Response(status_code=204)
    response.delete_cookie(COOKIE)
    return response


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser = Depends(current_user)) -> UserOut:
    return _out(user)
