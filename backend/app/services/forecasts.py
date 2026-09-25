"""Сигнатура сервиса (владелец BE-05). Тело — задача 3 плана каркаса, см. signatures.md."""
from datetime import date

from sqlalchemy.orm import Session

from ..schemas.common import Page
from ..schemas.forecasts import ForecastCard, ForecastItem


def list_forecasts(db: Session, *, scenario: str | None, date_from: date | None,
                   date_to: date | None, decision: str | None, outcome: str | None,
                   obj: str | None, group_by: str | None, page: int,
                   page_size: int) -> Page[ForecastItem]:
    raise NotImplementedError("каркас: задача 3")


def get_card(db: Session, forecast_id: str) -> ForecastCard | None:
    raise NotImplementedError("каркас: задача 3")
