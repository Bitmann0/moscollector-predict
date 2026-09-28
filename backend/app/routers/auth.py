"""Вход и текущий пользователь. Живое.

Частота входа ограничена здесь, а не в Caddy: в сборке caddy:2-alpine нет модуля
rate limit. Счётчик неудачных попыток живёт в процессе api (на стенде один процесс)
и сбрасывается при перезапуске — для перебора паролей этого достаточно.
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .. import models, vocab
from ..config import get_settings
from ..db import get_db
from ..schemas.auth import LoginIn, UserOut
from ..security import COOKIE, CurrentUser, current_user, issue_session, verify_password

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


@router.post("/auth/login", response_model=UserOut)
def login(body: LoginIn, request: Request, response: Response,
          db: Session = Depends(get_db)) -> UserOut:
    # За Caddy адрес клиента берётся из X-Forwarded-For (uvicorn --proxy-headers).
    key = (request.client.host if request.client else "", body.login)
    if throttle.blocked(key):
        raise HTTPException(status_code=429, detail="too_many_attempts")
    row = db.get(models.User, body.login)
    if row is None or not verify_password(body.password, row.password_hash):
        throttle.failed(key)
        raise HTTPException(status_code=401, detail="bad_credentials")
    throttle.succeeded(key)
    settings = get_settings()
    response.set_cookie(COOKIE, issue_session(row.login, row.password_hash), httponly=True, samesite="lax",
                        secure=settings.cookie_secure, max_age=settings.session_hours * 3600)
    user = CurrentUser(row.login, row.name, row.role, vocab.permissions_of(row.role))
    request.state.user = user
    return _out(user)


@router.post("/auth/logout", status_code=204)
def logout() -> Response:
    response = Response(status_code=204)
    response.delete_cookie(COOKIE)
    return response


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser = Depends(current_user)) -> UserOut:
    return _out(user)
