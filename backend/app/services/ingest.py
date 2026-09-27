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
import hashlib
import io
import zipfile
from datetime import date, datetime

import openpyxl
from fastapi import HTTPException
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import models
from ..schemas.common import Page
from ..schemas.events import EventRowIn, IngestBatchOut, OdsRowIn, ResetDayOut
from ..security import CurrentUser
from . import semantics, settings_store
from .helpers import assume_msk, count, from_db, now_utc, page_of, to_db

# Тот же лимит, что в routers/ingest.py: роутер читает на байт больше, чтобы сервис
# отличил файл ровно в лимит от файла больше лимита.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def _out(row: models.IngestBatch) -> IngestBatchOut:
    return IngestBatchOut(batch_id=row.id, kind=row.kind, received_at=from_db(row.received_at),
                          rows_total=row.rows_total, accepted=row.accepted,
                          duplicates=row.duplicates, rejected=row.rejected,
                          outside_demo_window=row.outside_demo_window, status=row.status)


def _new_batch(db: Session, kind: str, rows_total: int) -> models.IngestBatch:
    row = models.IngestBatch(kind=kind, received_at=now_utc(), rows_total=rows_total,
                             accepted=0, duplicates=0, rejected=0,
                             outside_demo_window=0, status="accepted")
    db.add(row)
    db.flush()
    return row


def _parse_alarm(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"t", "true", "1", "да"}:
        return True
    if normalized in {"f", "false", "0", "нет"}:
        return False
    raise ValueError("invalid_alarm")


def _event_from_row(row: EventRowIn, batch_id: int) -> models.Event:
    alarm = _parse_alarm(row.alarm)
    ts = assume_msk(datetime.fromisoformat(f"{row.day.strip()}T{row.time.strip()}"))
    raw = row.value.strip() if row.value else None
    try:
        value_num = float(raw.replace(",", ".")) if raw else None
    except ValueError:
        value_num = None
    sensor = None
    event_class, hint = semantics.classify(sensor, raw, value_num, alarm)
    identity = f"{row.event_id}|{row.channel_id}|{ts.isoformat()}|{alarm}|{raw or ''}"
    return models.Event(event_id=row.event_id, channel_id=row.channel_id, ts=to_db(ts),
                        alarm=alarm, val_raw=raw, val_num=value_num, event_class=event_class,
                        hint=hint, batch_id=batch_id,
                        row_hash=hashlib.sha256(identity.encode()).hexdigest())


def _save_rows(db: Session, rows: list[EventRowIn], user: CurrentUser) -> IngestBatchOut:
    batch = _new_batch(db, "smvu", len(rows))
    demo_today = settings_store.demo_today(db)
    for item in rows:
        try:
            event = _event_from_row(item, batch.id)
        except (TypeError, ValueError):
            batch.rejected += 1
            continue
        if from_db(event.ts).date() > demo_today:
            batch.outside_demo_window += 1
            continue
        if db.scalar(select(models.Event.id).where(models.Event.row_hash == event.row_hash)):
            batch.duplicates += 1
            continue
        db.add(event)
        batch.accepted += 1
    batch.status = "accepted" if not batch.rejected else ("partial" if batch.accepted else "rejected")
    db.commit()
    return _out(batch)


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
    return _save_rows(db, rows, user)


def ingest_file(db: Session, filename: str, content: bytes, user: CurrentUser) -> IngestBatchOut:
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file_too_large")
    try:
        rows_total = _count_rows(filename, content)
    except (ValueError, KeyError, OSError, csv.Error, zipfile.BadZipFile, InvalidFileException):
        batch = _new_batch(db, "smvu", 0)
        batch.status = "rejected"
        db.commit()
        return _out(batch)
    try:
        if filename.lower().endswith(".xlsx") or content[:2] == b"PK":
            book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            try:
                values = list(book.worksheets[0].iter_rows(values_only=True))
            finally:
                book.close()
            header, data = values[0], values[1:]
            records = [dict(zip(header, values)) for values in data if any(v is not None for v in values)]
        else:
            records = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        rows = [EventRowIn.model_validate(record) for record in records]
    except (IndexError, ValueError, TypeError):
        batch = _new_batch(db, "smvu", rows_total)
        batch.rejected = rows_total
        batch.status = "rejected"
        db.commit()
        return _out(batch)
    return _save_rows(db, rows, user)


def reset_day(db: Session, day: date, user: CurrentUser) -> ResetDayOut:
    start = to_db(assume_msk(datetime.combine(day, datetime.min.time())))
    end = to_db(assume_msk(datetime.combine(day.fromordinal(day.toordinal() + 1), datetime.min.time())))
    result = db.execute(delete(models.Event).where(models.Event.ts >= start, models.Event.ts < end))
    batch = _new_batch(db, "reset", 0)
    batch.accepted = result.rowcount or 0
    db.commit()
    return ResetDayOut(day=day, deleted_events=result.rowcount or 0)


def ingest_ods(db: Session, rows: list[OdsRowIn], user: CurrentUser) -> IngestBatchOut:
    batch = _new_batch(db, "ods", len(rows))
    for row in rows:
        db.add(models.OdsRecord(ts=to_db(assume_msk(row.ts)), obj_id=row.obj_id,
                                record_type=row.record_type, decision=row.decision,
                                reason=row.reason, batch_id=batch.id))
        batch.accepted += 1
    db.commit()
    return _out(batch)


def list_batches(db: Session, page: int, page_size: int) -> Page[IngestBatchOut]:
    stmt = select(models.IngestBatch)
    total = count(db, stmt)
    rows = db.scalars(stmt.order_by(models.IngestBatch.id.desc())
                      .offset((page - 1) * page_size).limit(page_size))
    return page_of(IngestBatchOut, [_out(r) for r in rows], total, page, page_size)
