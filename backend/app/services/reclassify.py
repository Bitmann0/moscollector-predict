"""Пересчёт класса, подсказки и группы аварии у принятых событий СМВУ по суткам МСК.

Приём ставит класс один раз, поэтому после смены правил C5 или параметров (ML2-13:
пороги метана, окна подсказок, серии) уже принятые события хранят прежние значения.
Пересчёт идёт теми же classify и series_hints из semantics.py, что приём.

Две точки входа с одним кодом суток (reclassify_day):
- POST /api/v1/admin/reclassify-events — период до RECLASSIFY_MAX_DAYS суток, для экрана
  «Настройки»; параметры — текущие из settings;
- scripts/reclassify_events.py — вся история или период, напрямую в БД; нужен на БД без
  миграции 0002 и там, где эндпоинта нет.

Сутки D: события [D − окно серии, D + 1 + окно серии] читаются одним запросом — запаса
хватает, чтобы подсказка серии на стыке суток была той же, что при приёме. UPDATE — только
у строк суток D, где изменились класс, подсказка или группа, одна транзакция на сутки:
прерванный пересчёт можно повторить, пересчитанные сутки дадут 0 изменений.
Скорость на стенде — 10 428 318 строк за 82,1 с (docs/submission/08-performance.md).
"""
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from .. import models
from ..schemas.parameters import ClassChange, ReclassifyIn, ReclassifyOut
from . import parameters, semantics
from .helpers import MSK, from_db, to_db

EVENTS = models.Event.__table__
_LOCK = threading.Lock()


@dataclass
class Totals:
    rows: int = 0
    changed: int = 0                                     # строк, где изменилось хоть что-то
    classes: Counter = field(default_factory=Counter)    # (было, стало), если класс сменился
    hints_changed: int = 0
    groups_changed: int = 0
    groups: Counter = field(default_factory=Counter)     # группа после пересчёта, все строки
    hints: Counter = field(default_factory=Counter)      # вид подсказки после пересчёта

    def add(self, other: "Totals") -> None:
        self.rows += other.rows
        self.changed += other.changed
        self.classes.update(other.classes)
        self.hints_changed += other.hints_changed
        self.groups_changed += other.groups_changed
        self.groups.update(other.groups)
        self.hints.update(other.hints)


def channel_map(conn) -> dict[int, tuple[str | None, str | None, str | None]]:
    rows = conn.execute(select(models.RefChannel.id, models.RefChannel.sensor_type,
                               models.RefChannel.obj_id, models.RefObject.parent_id)
                        .outerjoin(models.RefObject,
                                   models.RefObject.id == models.RefChannel.obj_id))
    return {cid: (st, obj, parent) for cid, st, obj, parent in rows}


def hint_kind(hint: str | None) -> str:
    if hint is None:
        return "нет"
    if hint.startswith(semantics.PPR_HINT + ": серия"):
        return "ППР/ТО, серия"
    if hint.startswith(semantics.PPR_HINT):
        return "ППР/ТО, газ в рабочие часы"
    return hint


def reclassify_day(conn, day: date, channels: dict, *, group_column: bool, write: bool,
                   rules: semantics.Rules = semantics.DEFAULT_RULES) -> Totals:
    start = datetime.combine(day, datetime.min.time(), MSK)
    end = start + timedelta(days=1)
    margin = rules.series_window
    group_col = EVENTS.c.incident_group if group_column else text("NULL")
    rows = conn.execute(select(EVENTS.c.id, EVENTS.c.channel_id, EVENTS.c.ts, EVENTS.c.alarm,
                               EVENTS.c.val_raw, EVENTS.c.val_num, EVENTS.c.event_class,
                               EVENTS.c.hint, group_col)
                        .where(EVENTS.c.ts >= to_db(start - margin),
                               EVENTS.c.ts < to_db(end + margin))).all()
    verdicts: dict[int, list] = {}
    stored: dict[int, tuple] = {}
    series: list[semantics.SeriesEvent] = []
    for row_id, channel_id, ts, alarm, val_raw, val_num, cls, hint, group in rows:
        sensor_type, obj_id, complex_id = channels.get(channel_id, (None, None, None))
        local = from_db(ts)
        verdict = semantics.classify(sensor_type, val_raw, val_num, alarm, ts=local,
                                     rules=rules)
        key = semantics.series_key(verdict.incident_group, obj_id, complex_id)
        if key is not None:
            series.append(semantics.SeriesEvent(ref=row_id, group=verdict.incident_group,
                                                key=key, channel_id=channel_id, ts=local))
        if start <= local < end:
            verdicts[row_id] = list(verdict)
            stored[row_id] = (cls, hint, group)
    for row_id, hint in semantics.series_hints(series, rules).items():
        if row_id in verdicts:
            verdicts[row_id][1] = hint
    totals = Totals(rows=len(verdicts))
    changes = []
    for row_id, (cls, hint, group) in verdicts.items():
        totals.groups[group] += 1
        totals.hints[hint_kind(hint)] += 1
        old_cls, old_hint, old_group = stored[row_id]
        if (cls, hint, group) != (old_cls, old_hint, old_group):
            if cls != old_cls:
                totals.classes[(old_cls, cls)] += 1
            totals.hints_changed += hint != old_hint
            totals.groups_changed += group != old_group
            changes.append({"row_id": row_id, "cls": cls, "hint": hint, "grp": group})
    totals.changed = len(changes)
    if write and changes:
        conn.execute(EVENTS.update().where(EVENTS.c.id == bindparam("row_id"))
                     .values(event_class=bindparam("cls"), hint=bindparam("hint"),
                             incident_group=bindparam("grp")), changes)
    return totals


def days_of(conn) -> tuple[date, date] | None:
    first, last = conn.execute(select(func.min(EVENTS.c.ts), func.max(EVENTS.c.ts))).one()
    if first is None:
        return None
    return from_db(first).date(), from_db(last).date()


def reclassify_period(db: Session, body: ReclassifyIn) -> ReclassifyOut:
    """Пересчёт периода по текущим параметрам. Второй вызов, пока идёт первый, — 409:
    два пересчёта одних суток писали бы одни и те же строки."""
    parameters.require_unlocked()
    if not _LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="reclassify_running")
    try:
        started = time.monotonic()
        rules = parameters.current(db).rules
        channels = channel_map(db.connection())
        totals = Totals()
        day = body.date_from
        while day <= body.date_to:
            totals.add(reclassify_day(db.connection(), day, channels, group_column=True,
                                      write=True, rules=rules))
            db.commit()
            day += timedelta(days=1)
    finally:
        _LOCK.release()
    return ReclassifyOut(
        date_from=body.date_from, date_to=body.date_to, rows=totals.rows,
        changed=totals.changed,
        classes=[ClassChange(old=old, new=new, count=n)
                 for (old, new), n in totals.classes.most_common()],
        hints_changed=totals.hints_changed, groups_changed=totals.groups_changed,
        seconds=round(time.monotonic() - started, 2))
