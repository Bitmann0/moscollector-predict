"""Сигнатура сервиса (владелец PM-09). Тело — задача 3 плана каркаса, см. signatures.md."""
from datetime import date

from sqlalchemy.orm import Session

from ..schemas.misc import RunDailyOut
from .ml_client import MlClient


def run_daily(db: Session, asof: date, ml: MlClient) -> RunDailyOut:
    raise NotImplementedError("каркас: задача 3")
