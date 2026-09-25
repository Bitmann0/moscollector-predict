"""Сигнатура сервиса (владелец BE-11). Тело — задача 3 плана каркаса, см. signatures.md."""
from datetime import date

from sqlalchemy.orm import Session


def forecasts_xlsx(db: Session, date_from: date | None, date_to: date | None) -> bytes:
    raise NotImplementedError("каркас: задача 3")
