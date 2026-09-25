"""Сигнатура сервиса (владелец ML2-03). Тело — задача 3 плана каркаса, см. signatures.md."""
from datetime import date

from sqlalchemy.orm import Session

from ..schemas.common import Page
from ..schemas.events import EventRowIn, IngestBatchOut, OdsRowIn, ResetDayOut
from ..security import CurrentUser


def ingest_rows(db: Session, rows: list[EventRowIn], user: CurrentUser) -> IngestBatchOut:
    raise NotImplementedError("каркас: задача 3")


def ingest_file(db: Session, filename: str, content: bytes, user: CurrentUser) -> IngestBatchOut:
    raise NotImplementedError("каркас: задача 3")


def reset_day(db: Session, day: date, user: CurrentUser) -> ResetDayOut:
    raise NotImplementedError("каркас: задача 3")


def ingest_ods(db: Session, rows: list[OdsRowIn], user: CurrentUser) -> IngestBatchOut:
    raise NotImplementedError("каркас: задача 3")


def list_batches(db: Session, page: int, page_size: int) -> Page[IngestBatchOut]:
    raise NotImplementedError("каркас: задача 3")
