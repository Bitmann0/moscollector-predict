"""Журнал событий СМВУ с фильтрами и сортировкой формы Приложения 2 ТЗ."""
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import and_, case, not_, or_, select
from sqlalchemy.orm import Session

from .. import models, vocab
from ..schemas.common import Page
from ..schemas.events import EventItem
from .helpers import Refs, assume_msk, count, from_db, page_of, to_db

# Штатные показания газовых датчиков — 80,5 % событий 2026 года (план команды, FE-06),
# поэтому журнал по умолчанию их скрывает. Фильтр живёт в запросе, а не в браузере:
# иначе он работал по последней загруженной странице, и из 1 000 строк оставалось восемь.
GAS_SENSOR = "газ"

Sort = Literal["ts", "object", "sensor_type", "sensor_event", "event_class"]
Order = Literal["asc", "desc"]
# По времени страницу отдаёт индекс ix_events_ts, не дольше 3 мс за любой период. Остальные
# колонки сортируются перебором всей выборки: объект и тип датчика лежат в справочниках,
# и индекс на events их не видит; индекс по классу или значению ускорил бы две колонки из
# четырёх. На стенде (10,4 млн событий) запрос по объекту без дат — 3,3 с вместе с count,
# за самую плотную неделю (1,35 млн, 20–26.05) по любой колонке — не больше 0,68 с
# (docs/submission/perf/events_sort_0928.txt). Отсюда предел: колонка вне времени —
# только при обеих датах и периоде не длиннее семи суток.
SORT_SPAN_DAYS = 7


def sort_range_ok(sort: Sort, date_from: date | None, date_to: date | None) -> bool:
    if sort == "ts":
        return True
    return bool(date_from and date_to and (date_to - date_from).days < SORT_SPAN_DAYS)


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
                incident_group: str | None = None,
                sort: Sort = "ts", order: Order = "desc") -> Page[EventItem]:
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
    stmt = _ordered(stmt, sort, order)
    rows = list(db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)))
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


def _ordered(stmt, sort: Sort, order: Order):
    """Порядок страницы. Пустые значения колонки — в конце при любом направлении.

    Внутри равных значений колонки события идут от новых к старым, как в журнале без
    сортировки. «Событие датчика»: сначала числа по величине, потом текст, чтобы по
    убыванию наверху оказались наибольшие показания, а «12» не стояло перед «2».
    «Тип события» — в порядке словаря C3, как в фильтре «Класс события».
    """
    desc = order == "desc"
    newest = (models.Event.ts.desc(), models.Event.id.desc())
    if sort == "ts":
        return stmt.order_by(*newest) if desc else stmt.order_by(models.Event.ts.asc(),
                                                                  models.Event.id.asc())

    def by(column):
        return (column.desc() if desc else column.asc()).nulls_last()

    if sort in ("object", "sensor_type"):
        stmt = stmt.outerjoin(models.RefChannel, models.RefChannel.id == models.Event.channel_id)
    if sort == "object":
        stmt = stmt.outerjoin(models.RefObject, models.RefObject.id == models.RefChannel.obj_id)
        keys = [by(models.RefObject.name)]
    elif sort == "sensor_type":
        keys = [by(models.RefChannel.sensor_type)]
    elif sort == "sensor_event":
        keys = [by(models.Event.val_num), by(models.Event.val_raw)]
    else:
        rank = case({code: n for n, code in enumerate(vocab.codes("event_class"))},
                    value=models.Event.event_class)
        keys = [by(rank)]
    return stmt.order_by(*keys, *newest)
