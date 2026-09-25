"""Сигнатура сервиса (владелец BE-05). Тело — задача 3 плана каркаса, см. signatures.md."""
from sqlalchemy.orm import Session

from ..schemas.dashboard import DashboardSummary


def summary(db: Session) -> DashboardSummary:
    raise NotImplementedError("каркас: задача 3")
