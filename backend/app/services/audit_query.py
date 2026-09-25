"""Сигнатура сервиса (владелец BE-09). Тело — задача 3 плана каркаса, см. signatures.md."""
from datetime import date

from sqlalchemy.orm import Session

from ..schemas.common import Page
from ..schemas.misc import AuditItem


def list_audit(db: Session, *, user_login: str | None, date_from: date | None,
               date_to: date | None, page: int, page_size: int) -> Page[AuditItem]:
    raise NotImplementedError("каркас: задача 3")
