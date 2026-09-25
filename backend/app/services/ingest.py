"""Приём журнала СМВУ и журнала ОДС, сброс дня, история загрузок.

ЗАГЛУШКА — владелец ML2-03 (C2, C5); ingest_ods — владелец BE-06.
Заменить: разбор строк (t/f, дата + время в МСК, число из значения), запись в events
с дедупликацией по row_hash и классификацией semantics.classify, подсчёт duplicates,
rejected и outside_demo_window, SSE event.alarm для классов alarm и critical; запись
ods_journal; удаление событий дня в reset_day.
Контракт: сигнатуры и IngestBatchOut не меняются; тест tests/test_endpoints_shape.py
должен остаться зелёным.

Сейчас строки не пишутся: партия получает accepted = rows_total и статус accepted, но
сама строка ingest_batches записывается, чтобы batch_id был настоящим. Файл только
считается по строкам: XLSX — через openpyxl, иначе CSV в UTF-8.
"""
import csv
import io
import zipfile
from datetime import date

import openpyxl
from fastapi import HTTPException
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas.common import Page
from ..schemas.events import EventRowIn, IngestBatchOut, OdsRowIn, ResetDayOut
from ..security import CurrentUser
from .helpers import count, from_db, now_utc, page_of

# Тот же лимит, что в routers/ingest.py: роутер читает на байт больше, чтобы сервис
# отличил файл ровно в лимит от файла больше лимита.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def _out(row: models.IngestBatch) -> IngestBatchOut:
    return IngestBatchOut(batch_id=row.id, kind=row.kind, received_at=from_db(row.received_at),
                          rows_total=row.rows_total, accepted=row.accepted,
                          duplicates=row.duplicates, rejected=row.rejected,
                          outside_demo_window=row.outside_demo_window, status=row.status)


def _batch(db: Session, kind: str, rows_total: int, status: str = "accepted") -> IngestBatchOut:
    row = models.IngestBatch(kind=kind, received_at=now_utc(), rows_total=rows_total,
                             accepted=rows_total if status == "accepted" else 0,
                             duplicates=0, rejected=0 if status == "accepted" else rows_total,
                             outside_demo_window=0, status=status)
    db.add(row)
    db.commit()
    return _out(row)


def _count_rows(filename: str, content: bytes) -> int:
    if filename.lower().endswith(".xlsx") or content[:2] == b"PK":
        book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            sheet = book.worksheets[0]
            rows = sum(1 for r in sheet.iter_rows(values_only=True)
                       if any(v is not None for v in r))
        finally:
            book.close()
    else:
        text = content.decode("utf-8-sig")
        rows = sum(1 for r in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in r))
    return max(rows - 1, 0)  # первая строка — заголовок


def ingest_rows(db: Session, rows: list[EventRowIn], user: CurrentUser) -> IngestBatchOut:
    return _batch(db, "smvu", len(rows))


def ingest_file(db: Session, filename: str, content: bytes, user: CurrentUser) -> IngestBatchOut:
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file_too_large")
    try:
        rows_total = _count_rows(filename, content)
    except (ValueError, KeyError, OSError, csv.Error, zipfile.BadZipFile, InvalidFileException):
        return _batch(db, "smvu", 0, status="rejected")
    return _batch(db, "smvu", rows_total)


def reset_day(db: Session, day: date, user: CurrentUser) -> ResetDayOut:
    return ResetDayOut(day=day, deleted_events=0)


def ingest_ods(db: Session, rows: list[OdsRowIn], user: CurrentUser) -> IngestBatchOut:
    return _batch(db, "ods", len(rows))


def list_batches(db: Session, page: int, page_size: int) -> Page[IngestBatchOut]:
    stmt = select(models.IngestBatch)
    total = count(db, stmt)
    rows = db.scalars(stmt.order_by(models.IngestBatch.id.desc())
                      .offset((page - 1) * page_size).limit(page_size))
    return page_of(IngestBatchOut, [_out(r) for r in rows], total, page, page_size)
