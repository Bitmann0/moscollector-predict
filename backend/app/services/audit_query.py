"""Журнал действий пользователей (ТЗ §11). Живое; BE-09 расширяет охват записи.

Фильтры: логин, дни from–to по московскому времени включительно. Порядок — новые
сверху.
"""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas.common import Page
from ..schemas.misc import AuditItem
from .helpers import count, from_db, msk_midnight, page_of, to_db


def list_audit(db: Session, *, user_login: str | None, date_from: date | None,
               date_to: date | None, page: int, page_size: int) -> Page[AuditItem]:
    stmt = select(models.AuditRecord)
    if user_login is not None:
        stmt = stmt.where(models.AuditRecord.user_login == user_login)
    if date_from is not None:
        stmt = stmt.where(models.AuditRecord.ts >= to_db(msk_midnight(date_from)))
    if date_to is not None:
        stmt = stmt.where(models.AuditRecord.ts < to_db(msk_midnight(date_to + timedelta(1))))
    total = count(db, stmt)
    rows = db.scalars(stmt.order_by(models.AuditRecord.ts.desc(), models.AuditRecord.id.desc())
                      .offset((page - 1) * page_size).limit(page_size))
    items = [AuditItem(id=r.id, ts=from_db(r.ts), user_login=r.user_login, role=r.role,
                       method=r.method, path=r.path, status=r.status, entity=r.entity)
             for r in rows]
    return page_of(AuditItem, items, total, page, page_size)
