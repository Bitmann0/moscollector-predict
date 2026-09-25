"""Сигнатура сервиса (владелец ML2-03). Тело — задача 3 плана каркаса, см. signatures.md."""
from datetime import date

from sqlalchemy.orm import Session

from ..schemas.common import Page
from ..schemas.events import EventItem


def list_events(db: Session, *, date_from: date | None, date_to: date | None,
                obj: str | None, sensor_type: str | None, event_class: str | None,
                q: str | None, page: int, page_size: int) -> Page[EventItem]:
    raise NotImplementedError("каркас: задача 3")
