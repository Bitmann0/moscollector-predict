"""Журнал событий СМВУ (форма Приложения 2 ТЗ).

ЗАГЛУШКА — владелец ML2-03 (C2, C5).
Заменить: выборку из таблицы events с фильтрами from, to, obj, sensor_type,
event_class и поиском q — сейчас фильтры принимаются и не применяются, а страница
собирается из 500 синтетических событий по contracts/synthetic_reference.json
(каждое седьмое — тревога, шаг 7 минут назад от конца demo_today).
Контракт: list_events и EventItem не меняются; тест tests/test_endpoints_shape.py
должен остаться зелёным.
"""
from datetime import date, timedelta

from sqlalchemy.orm import Session

from .. import vocab
from ..schemas.common import ChannelRef, ObjectRef, Page
from ..schemas.events import EventItem
from . import semantics, settings_store
from .helpers import msk_midnight, page_of, picket_label, synthetic_reference

SYNTHETIC_TOTAL = 500
STEP = timedelta(minutes=7)
ALARM_EVERY = 7


def _synthetic_event(index: int, end, objects: dict, channels: list[dict]) -> EventItem:
    channel = channels[index % len(channels)]
    obj = objects.get(channel["obj_id"], {})
    parent = objects.get(obj.get("parent_id"), {})
    alarm = index % ALARM_EVERY == 0
    sensor_event = "Сработка (заглушка)" if alarm else "Норма (заглушка)"
    event_class, hint = semantics.classify(channel.get("sensor_type"), sensor_event, None, alarm)
    return EventItem(
        id=index + 1,
        ts=end - STEP * (index + 1),
        object=ObjectRef(id=obj.get("id"), name=obj.get("name"), complex_id=parent.get("id"),
                         complex_name=parent.get("name")),
        channel=ChannelRef(id=channel["id"], name=channel.get("name"),
                           sensor_type=channel.get("sensor_type"),
                           picket_label=picket_label(channel.get("picket"))),
        sensor_event=sensor_event,
        event_class=event_class,
        event_class_title=vocab.title("event_class", event_class),
        hint=hint,
        alarm=alarm,
    )


def list_events(db: Session, *, date_from: date | None, date_to: date | None,
                obj: str | None, sensor_type: str | None, event_class: str | None,
                q: str | None, page: int, page_size: int) -> Page[EventItem]:
    ref = synthetic_reference()
    objects = {o["id"]: o for o in ref["objects"]}
    end = msk_midnight(settings_store.demo_today(db) + timedelta(days=1))
    start = (page - 1) * page_size
    stop = min(start + page_size, SYNTHETIC_TOTAL)
    items = [_synthetic_event(i, end, objects, ref["channels"]) for i in range(start, stop)]
    return page_of(EventItem, items, SYNTHETIC_TOTAL, page, page_size)
