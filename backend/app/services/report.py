"""Данные отчёта руководству за период (ТЗ §8); вёрстку в PDF делает report_pdf.py.

Числа по сценариям берутся из forecasts.summary — того же итога, что отдаёт
GET /forecasts/summary и что через quality.tally считает экран «Качество»: у отчёта
своих правил подсчёта нет. Прогнозы и заявки попадают в период по дате расчёта asof
(C1, МСК), события — по времени события; сутки режутся по полуночи МСК.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .. import models, vocab
from . import forecasts, semantics, settings_store
from .helpers import MSK, Refs, from_db, msk_midnight, now_utc, to_db

DEFAULT_DAYS = 7
# Столбец на сутки: за год их 366, и шире диаграмма на листе A4 не читается.
MAX_DAYS = 366
TOP_OBJECTS = 10


@dataclass
class ScenarioRow:
    code: str
    title: str
    issued: int
    hit: int
    miss: int
    unknown: int
    decided: int

    @property
    def known(self) -> int:
        """С фактом: попадание или промах. «Неизвестно» в точность не входит, как в «Качестве»."""
        return self.hit + self.miss

    @property
    def precision(self) -> float | None:
        return self.hit / self.known if self.known else None


@dataclass
class IncidentRow:
    code: str
    title: str
    alarms: int
    planned_like: int  # с подсказкой «вероятно, ППР или ТО» (semantics.PPR_HINT)


@dataclass
class ObjectRow:
    obj_id: str
    name: str | None
    complex_name: str | None
    forecasts: int
    incidents: int

    @property
    def total(self) -> int:
        return self.forecasts + self.incidents


@dataclass
class DayPoint:
    day: date
    forecasts: int
    calculated: bool  # был дневной расчёт; без него столбца нет, а не ноль


@dataclass
class ReportData:
    date_from: date
    date_to: date
    demo_today: date
    generated_at: datetime
    scenarios: list[ScenarioRow]
    orders_by_status: list[tuple[str, int]]  # (название статуса, заявок)
    incidents: list[IncidentRow]
    incidents_without_object: int
    alarms: IncidentRow  # все тревожные сообщения периода, с группой аварии и без
    top_objects: list[ObjectRow]
    days: list[DayPoint]
    emulated_decided: int

    @property
    def days_total(self) -> int:
        return (self.date_to - self.date_from).days + 1


def period(db: Session, date_from: date | None, date_to: date | None) -> tuple[date, date]:
    """Без to — по демо-дату включительно, без from — семь суток до to включительно."""
    date_to = date_to or settings_store.demo_today(db)
    date_from = date_from or date_to - timedelta(days=DEFAULT_DAYS - 1)
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="from_after_to")
    if (date_to - date_from).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=422, detail="period_too_long")
    return date_from, date_to


def _scenarios(db: Session, date_from: date, date_to: date) -> list[ScenarioRow]:
    rows = []
    for s in vocab.load()["scenario"]:
        weeks = forecasts.summary(db, scenario=s["code"], date_from=date_from, date_to=date_to,
                                  decision=None, outcome=None, obj=None, group_by=None).weeks
        rows.append(ScenarioRow(
            code=s["code"], title=s["title"], issued=sum(w.issued for w in weeks),
            hit=sum(w.hit for w in weeks), miss=sum(w.miss for w in weeks),
            unknown=sum(w.unknown for w in weeks), decided=sum(w.decided for w in weeks)))
    return rows


def _in_period(date_from: date, date_to: date) -> tuple:
    return (models.Forecast.in_budget.is_(True), models.Forecast.asof >= date_from,
            models.Forecast.asof <= date_to)


def _emulated_decided(db: Session, date_from: date, date_to: date) -> int:
    """Прогнозы периода, у которых есть эмулированное решение (emulation.SOURCE)."""
    ids = select(models.Forecast.id).where(*_in_period(date_from, date_to))
    return db.scalar(select(func.count(func.distinct(models.Decision.forecast_id))).where(
        models.Decision.forecast_id.in_(ids), models.Decision.source == "emulated")) or 0


def _orders_by_status(db: Session, date_from: date, date_to: date) -> list[tuple[str, int]]:
    """Заявки по прогнозам периода, статус — на момент формирования отчёта.

    created_at заявки — настоящее время записи: на стенде все заявки созданы прелоадом
    28.09, и по нему июнь пуст. Поэтому заявка относится ко дню расчёта самого раннего
    своего прогноза, а без прогнозов в БД — ко дню создания по МСК. Заявок в демо
    десятки (work_order_ids читает их так же, целиком).
    """
    orders = db.execute(select(models.WorkOrder.status, models.WorkOrder.forecast_ids,
                               models.WorkOrder.created_at)).all()
    ids = {fid for _, stored, _ in orders for fid in stored or []}
    asof = dict(db.execute(select(models.Forecast.id, models.Forecast.asof)
                           .where(models.Forecast.id.in_(ids))).all()) if ids else {}
    counts = dict.fromkeys(vocab.codes("work_order_status"), 0)
    for status, stored, created_at in orders:
        days = [asof[fid] for fid in stored or [] if fid in asof]
        day = min(days) if days else from_db(created_at).date()
        if date_from <= day <= date_to:
            counts[status] = counts.get(status, 0) + 1
    return [(vocab.title("work_order_status", code), n) for code, n in counts.items()]


def _events_in(date_from: date, date_to: date) -> tuple:
    return (models.Event.alarm.is_(True), models.Event.ts >= to_db(msk_midnight(date_from)),
            models.Event.ts < to_db(msk_midnight(date_to + timedelta(days=1))))


PLANNED = models.Event.hint.startswith(semantics.PPR_HINT, autoescape=True)


def _alarms(db: Session, date_from: date, date_to: date) -> IncidentRow:
    """Все тревожные сообщения периода, с группой аварии и без.

    Самый долгий запрос отчёта: идёт по ix_events_ts через все события периода. На копии
    стенда — 85 мс за семь суток и 304 мс за июнь (docs/submission/perf/report_pdf_0928.txt).
    """
    total, planned = db.execute(select(func.count(), func.sum(case((PLANNED, 1), else_=0)))
                                .where(*_events_in(date_from, date_to))).one()
    return IncidentRow(code="all", title="Все тревожные сообщения", alarms=total or 0,
                       planned_like=planned or 0)


def _incidents(db: Session, date_from: date, date_to: date) -> list[tuple]:
    """(группа, объект, тревожных сообщений, с подсказкой ППР/ТО).

    Группа есть у 13 637 из 10,4 млн событий стенда, и PostgreSQL пересекает частичный
    индекс ix_events_incident_group с ix_events_ts: 39 мс за семь суток на копии стенда
    (docs/submission/perf/report_pdf_0928.txt). Объект — через справочник каналов; у канала
    без строки в справочнике объект None.
    """
    return db.execute(
        select(models.Event.incident_group, models.RefChannel.obj_id, func.count(),
               func.sum(case((PLANNED, 1), else_=0)))
        .select_from(models.Event)
        .outerjoin(models.RefChannel, models.RefChannel.id == models.Event.channel_id)
        .where(models.Event.incident_group.is_not(None), *_events_in(date_from, date_to))
        .group_by(models.Event.incident_group, models.RefChannel.obj_id)).all()


def _top_objects(db: Session, date_from: date, date_to: date,
                 incidents_by_obj: dict[str, int]) -> list[ObjectRow]:
    """Порядок — по сумме прогнозов и тревожных сообщений групп аварий, затем по прогнозам."""
    by_obj = dict(db.execute(select(models.Forecast.obj_id, func.count())
                             .where(*_in_period(date_from, date_to),
                                    models.Forecast.obj_id.is_not(None))
                             .group_by(models.Forecast.obj_id)).all())
    keys = set(by_obj) | set(incidents_by_obj)
    ranked = sorted(keys, key=lambda k: (-(by_obj.get(k, 0) + incidents_by_obj.get(k, 0)),
                                         -by_obj.get(k, 0), k))[:TOP_OBJECTS]
    refs = Refs(db)
    rows = []
    for obj_id in ranked:
        address = None
        if obj_id not in refs.objects:
            # Объекта нет в справочнике: название берём из адреса прогноза, как журнал.
            address = db.scalar(select(models.Forecast.address).where(
                models.Forecast.obj_id == obj_id).order_by(models.Forecast.asof.desc()).limit(1))
        ref = refs.object_ref(obj_id, address)
        rows.append(ObjectRow(obj_id=obj_id, name=ref.name, complex_name=ref.complex_name,
                              forecasts=by_obj.get(obj_id, 0),
                              incidents=incidents_by_obj.get(obj_id, 0)))
    return rows


def _days(db: Session, date_from: date, date_to: date) -> list[DayPoint]:
    """Выдано прогнозов по дням расчёта; правило дня без расчёта — как у ряда дашборда."""
    counts = dict(db.execute(select(models.Forecast.asof, func.count())
                             .where(*_in_period(date_from, date_to))
                             .group_by(models.Forecast.asof)).all())
    run_days = set(db.scalars(select(models.ForecastRun.asof).where(
        models.ForecastRun.kind == "daily", models.ForecastRun.asof >= date_from,
        models.ForecastRun.asof <= date_to)))
    days = [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]
    return [DayPoint(day=d, forecasts=counts.get(d, 0),
                     calculated=d in run_days or counts.get(d, 0) > 0) for d in days]


def collect(db: Session, date_from: date, date_to: date) -> ReportData:
    incidents = _incidents(db, date_from, date_to)
    by_group: dict[str, list[int]] = {}
    by_obj: dict[str, int] = {}
    without_object = 0
    for group, obj_id, alarms, planned in incidents:
        totals = by_group.setdefault(group, [0, 0])
        totals[0] += alarms
        totals[1] += planned or 0
        if obj_id is None:
            without_object += alarms
        else:
            by_obj[obj_id] = by_obj.get(obj_id, 0) + alarms
    return ReportData(
        date_from=date_from, date_to=date_to, demo_today=settings_store.demo_today(db),
        generated_at=now_utc().astimezone(MSK),
        scenarios=_scenarios(db, date_from, date_to),
        orders_by_status=_orders_by_status(db, date_from, date_to),
        incidents=[IncidentRow(code=item["code"], title=item["title"],
                               alarms=by_group.get(item["code"], [0, 0])[0],
                               planned_like=by_group.get(item["code"], [0, 0])[1])
                   for item in vocab.load()["incident_group"]],
        incidents_without_object=without_object,
        alarms=_alarms(db, date_from, date_to),
        top_objects=_top_objects(db, date_from, date_to, by_obj),
        days=_days(db, date_from, date_to),
        emulated_decided=_emulated_decided(db, date_from, date_to),
    )
