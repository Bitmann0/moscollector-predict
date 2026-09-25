"""Журнал действий пользователей (ТЗ §11). Живое, минимум; BE-09 расширяет охват.

Пишется каждый изменяющий запрос (POST, PUT, PATCH, DELETE) и выгрузки
(GET /export, GET /audit): кто, метод, путь, код ответа. Запись идёт в своей
сессии БД, чтобы откат транзакции запроса не стирал след попытки.
"""
import logging
from datetime import UTC, datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from . import models
from .db import session_factory

log = logging.getLogger(__name__)

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
AUDITED_READS = ("/api/v1/export", "/api/v1/audit")


def should_audit(method: str, path: str) -> bool:
    if not path.startswith("/api/v1"):
        return False
    return method in MUTATING or path.startswith(AUDITED_READS)


def _entity(request: Request) -> str | None:
    """Первый параметр пути (id прогноза, заявки, день) — чтобы запись находилась по объекту."""
    params = request.path_params or {}
    value = next(iter(params.values()), None)
    return None if value is None else str(value)[:200]


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if should_audit(request.method, request.url.path):
            user = getattr(request.state, "user", None)
            try:
                with session_factory()() as db:
                    db.add(models.AuditRecord(
                        ts=datetime.now(UTC),
                        user_login=getattr(user, "login", None),
                        role=getattr(user, "role", None),
                        method=request.method,
                        path=request.url.path,
                        status=response.status_code,
                        entity=_entity(request),
                    ))
                    db.commit()
            except Exception:  # аудит не должен ронять ответ пользователю
                log.exception("audit write failed")
        return response
