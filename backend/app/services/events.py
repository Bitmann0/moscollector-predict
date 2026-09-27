"""Журнал событий СМВУ (форма Приложения 2 ТЗ).

ЗАГЛУШКА — владелец ML2-03 (C2, C5).
Заменить: выборку из таблицы events с фильтрами from, to, obj, sensor_type,
event_class и поиском q — сейчас фильтры принимаются и не применяются, а страница
собирается из 500 синтетических событий по contracts/synthetic_reference.json
(каждое седьмое — тревога, шаг 7 минут назад от конца demo_today).
Контракт: list_events и EventItem не меняются; тест tests/test_endpoints_shape.py
должен остаться зелёным.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, vocab
from ..schemas.common import Page
from ..schemas.events import EventItem
from .helpers import Refs, assume_msk, count, from_db, page_of, to_db


def list_events(db: Session, *, date_from: date | None, date_to: date | None,
                obj: str | None, sensor_type: str | None, event_class: str | None,
                q: str | None, page: int, page_size: int) -> Page[EventItem]:
    stmt = select(models.Event)
    if date_from:
        stmt = stmt.where(models.Event.ts >= to_db(assume_msk(datetime.combine(date_from, datetime.min.time()))))
    if date_to:
        stmt = stmt.where(models.Event.ts < to_db(assume_msk(datetime.combine(
            date_to + timedelta(days=1), datetime.min.time()))))
    if event_class:
        stmt = stmt.where(models.Event.event_class == event_class)
    if q:
        stmt = stmt.where(models.Event.val_raw.ilike(f"%{q}%"))
    if obj or sensor_type:
        channel_ids = select(models.RefChannel.id)
        if obj:
            channel_ids = channel_ids.where(models.RefChannel.obj_id == obj)
        if sensor_type:
            channel_ids = channel_ids.where(models.RefChannel.sensor_type == sensor_type)
        stmt = stmt.where(models.Event.channel_id.in_(channel_ids))
    total = count(db, stmt)
    rows = list(db.scalars(stmt.order_by(models.Event.ts.desc(), models.Event.id.desc())
                           .offset((page - 1) * page_size).limit(page_size)))
    refs = Refs(db, {row.channel_id for row in rows})
    items = [EventItem(id=row.id, ts=from_db(row.ts),
                       object=refs.object_ref(refs.channels.get(row.channel_id).obj_id
                                               if row.channel_id in refs.channels else None),
                       channel=refs.channel_ref(row.channel_id), sensor_event=row.val_raw,
                       event_class=row.event_class,
                       event_class_title=vocab.title("event_class", row.event_class),
                       hint=row.hint, alarm=row.alarm) for row in rows]
    return page_of(EventItem, items, total, page, page_size)
