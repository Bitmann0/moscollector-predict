"""Журнал действий пользователей (ТЗ §11).

Пишется каждый изменяющий запрос (POST, PUT, PATCH, DELETE) и выгрузки
(GET /export, GET /audit): кто, метод, путь, код ответа. Запись идёт в своей
сессии БД, чтобы откат транзакции запроса не стирал след попытки, и в пуле
потоков, чтобы не останавливать цикл событий. Необработанное исключение
обработчика записывается как 500 и пробрасывается дальше. Анонимные 404/405
(перебор несуществующих путей) не пишутся: иначе их поток забивает журнал.

Логин берётся из request.state.user. У неудачного входа пользователя нет, и пишется
логин, который пробовали (request.state.login_attempt, ставит routers/auth.py), без
роли: по журналу видно, какую учётную запись перебирали. Выход кладёт в state
пользователя, чью сессию отозвал. Тело запроса, а с ним и пароль, не пишется.
Что изменилось, обработчик кладёт в request.state.audit_payload, и оно идёт в
audit_log.payload: PUT /settings/parameters — поля «было → стало», пересчёт истории —
период и число изменённых строк.
"""
import logging
from datetime import UTC, datetime

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from . import models
from .db import session_factory

log = logging.getLogger(__name__)

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
AUDITED_READS = ("/api/v1/export", "/api/v1/audit")
PATH_MAX = 500    # длина колонки audit_log.path
ENTITY_MAX = 200  # длина колонки audit_log.entity


def should_audit(method: str, path: str) -> bool:
    if not path.startswith("/api/v1"):
        return False
    return method in MUTATING or path.startswith(AUDITED_READS)


def _entity(request: Request) -> str | None:
    """Первый параметр пути (id прогноза, заявки, день) — чтобы запись находилась по объекту."""
    params = request.path_params or {}
    value = next(iter(params.values()), None)
    return None if value is None else str(value)[:ENTITY_MAX]


def _write(record: dict) -> None:
    try:
        with session_factory()() as db:
            db.add(models.AuditRecord(**record))
            db.commit()
    except Exception:  # аудит не должен ронять ответ пользователю
        log.exception("audit write failed: %s %s", record.get("method"), record.get("path"))


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not should_audit(request.method, request.url.path):
            return await call_next(request)
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            user = getattr(request.state, "user", None)
            if user is not None or status not in (404, 405):
                login = (user.login if user is not None
                         else getattr(request.state, "login_attempt", None))
                await run_in_threadpool(_write, {
                    "ts": datetime.now(UTC),
                    "user_login": login,
                    "role": getattr(user, "role", None),
                    "method": request.method,
                    "path": request.url.path[:PATH_MAX],
                    "status": status,
                    "entity": _entity(request),
                    "payload": getattr(request.state, "audit_payload", None),
                })
