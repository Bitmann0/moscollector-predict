"""Журнал событий СМВУ с фильтрами формы Приложения 2 ТЗ."""
from datetime import date, datetime, timedelta

from sqlalchemy import and_, not_, or_, select
from sqlalchemy.orm import Session

from .. import models, vocab
from ..schemas.common import Page
from ..schemas.events import EventItem
from .helpers import Refs, assume_msk, count, from_db, page_of, to_db

# Штатные показания газовых датчиков — 80,5 % событий 2026 года (план команды, FE-06),
# поэтому журнал по умолчанию их скрывает. Фильтр живёт в запросе, а не в браузере:
# иначе он работал по последней загруженной странице, и из 1 000 строк оставалось восемь.
GAS_SENSOR = "газ"


def _contains(column, text: str):
    """Подстрока без учёта регистра первой буквы.

    ilike на SQLite сворачивает регистр только у латиницы, а «газ» надо найти и в
    «Газовый датчик». Поэтому вариантов три: как ввёл пользователь, с заглавной и строчными.
    """
    variants = {text, text[:1].upper() + text[1:], text.lower()}
    return or_(*(column.like(f"%{v}%") for v in variants))


def list_events(db: Session, *, date_from: date | None, date_to: date | None,
                obj: str | None, sensor_type: str | None, event_class: str | None,
                q: str | None, page: int, page_size: int,
                hide_normal_gas: bool = False,
                incident_group: str | None = None) -> Page[EventItem]:
    stmt = select(models.Event)
    if date_from:
        stmt = stmt.where(models.Event.ts >= to_db(assume_msk(datetime.combine(date_from, datetime.min.time()))))
    if date_to:
        stmt = stmt.where(models.Event.ts < to_db(assume_msk(datetime.combine(
            date_to + timedelta(days=1), datetime.min.time()))))
    if event_class:
        stmt = stmt.where(models.Event.event_class == event_class)
    if incident_group:
        stmt = stmt.where(models.Event.incident_group == incident_group)
    if q:
        stmt = stmt.where(models.Event.val_raw.ilike(f"%{q}%"))
    if obj or sensor_type:
        channel_ids = select(models.RefChannel.id)
        if obj:
            # Поле формы — «Название или ID»: точный id или часть названия объекта.
            named = select(models.RefObject.id).where(_contains(models.RefObject.name, obj))
            channel_ids = channel_ids.where(or_(models.RefChannel.obj_id == obj,
                                                models.RefChannel.obj_id.in_(named)))
        if sensor_type:
            channel_ids = channel_ids.where(_contains(models.RefChannel.sensor_type, sensor_type))
        stmt = stmt.where(models.Event.channel_id.in_(channel_ids))
    if hide_normal_gas:
        gas = select(models.RefChannel.id).where(_contains(models.RefChannel.sensor_type, GAS_SENSOR))
        stmt = stmt.where(not_(and_(models.Event.event_class == "normal",
                                    models.Event.channel_id.in_(gas))))
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
                       incident_group=row.incident_group, hint=row.hint,
                       alarm=row.alarm) for row in rows]
    return page_of(EventItem, items, total, page, page_size)
