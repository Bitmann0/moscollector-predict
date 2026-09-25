"""Сигнатура сервиса (владелец BE-05). Тело — задача 3 плана каркаса, см. signatures.md."""
from sqlalchemy.orm import Session

from ..schemas.misc import QualityOut


def weekly(db: Session, scenario: str) -> QualityOut:
    raise NotImplementedError("каркас: задача 3")
