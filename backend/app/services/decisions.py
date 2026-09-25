"""Сигнатура сервиса (владелец BE-06). Тело — задача 3 плана каркаса, см. signatures.md."""
from sqlalchemy.orm import Session

from ..schemas.forecasts import DecisionIn, DecisionOut, OutcomeIn, OutcomeOut
from ..security import CurrentUser


def create(db: Session, forecast_id: str, body: DecisionIn, user: CurrentUser) -> DecisionOut:
    raise NotImplementedError("каркас: задача 3")


def set_outcome(db: Session, forecast_id: str, body: OutcomeIn, user: CurrentUser) -> OutcomeOut:
    raise NotImplementedError("каркас: задача 3")
