"""Сигнатура сервиса (владелец BE-05). Тело — задача 3 плана каркаса, см. signatures.md."""
from sqlalchemy.orm import Session

from ..schemas.system import SystemStatus
from .ml_client import MlClient


def status(db: Session, ml: MlClient) -> SystemStatus:
    raise NotImplementedError("каркас: задача 3")
