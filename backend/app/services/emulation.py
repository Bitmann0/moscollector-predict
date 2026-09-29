"""Эмулированные решения диспетчера и итоги проверки по факту СМВУ (PM-09). Живое.

Журнала решений ОДС в данных заказчика нет: раздел 3 плана команды велит засеять его
самим и пометить source=emulated. Правила засева:
- берутся прогнозы окна в бюджете, у которых уже есть автоматический факт; у открытых
  окон решения нет — на показе их решает человек;
- решение получает доля share: прогноз выбирается хешем своего id, поэтому выбор не
  зависит от порядка строк и повторяется от запуска к запуску;
- hit — выезд или удалённая проверка с подтверждением, итог confirmed_event;
  miss — reject / false_alarm, итог no_event; unknown — defer / await_data без итога;
- прогноз с решением или итогом человека (source ≠ emulated) не трогается целиком;
- повтор сводит эмуляцию к тому же набору: совпавшее решение остаётся с прежним id,
  лишнее удаляется. Если факт сменился (stub → real), решения следуют за ним.

Заявки идут за решениями, иначе к демо-дню в списке висят одни просроченные черновики:
учётной системы нет, а emulate_helpdesk.py двигает только подтверждённые и ставит время
запуска, а не демо-время. Если среди прогнозов заявки есть попадание — черновик
подтверждается в момент решения, через час уходит в работу и закрыт к итогу проверки;
если все её прогнозы решены как ложные — заявка отменяется; иначе остаётся черновиком.
Заявка, которую двигал человек, не трогается; свою историю эмуляция при повторе
переписывает заново.
"""
import hashlib
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas.misc import EmulateDecisionsIn, EmulateDecisionsOut
from .helpers import from_db, to_db

SOURCE = "emulated"
AUTHOR = "emulator"
COMMENT = "эмуляция: решение засеяно прелоадом по факту СМВУ"
DECIDED_FROM_H = 8      # решение — утром первого дня окна, 08:00–12:00 МСК
DECIDED_SPREAD_MIN = 240
CHECKED_AFTER_H = 3     # итог проверки — через 3 ч после решения
IN_WORK_AFTER_H = 1     # заявка по подтверждённому прогнозу — в работе через час
HIT_ACTIONS = {"dispatch_crew", "remote_check"}
ORDER_REASON = {
    "confirmed": "эмуляция: заявка подтверждена по решению диспетчера",
    "in_progress": "эмуляция help desk: бригада приняла заявку в работу",
    "completed": "эмуляция help desk: работы выполнены, заявка закрыта",
    "cancelled": "эмуляция: все прогнозы заявки признаны ложными срабатываниями",
}


def _u(*parts) -> float:
    """Детерминированное U[0, 1) по строке: одинаково в любом процессе и на любой БД."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def selected(forecast_id: str, share: float) -> bool:
    """Попадает ли прогноз в эмулируемую долю. Выбор при share 0,3 входит в выбор при 0,6."""
    return _u(SOURCE, forecast_id) < share


def plan(row: models.Forecast, fact: str) -> tuple[str, str, str | None]:
    """(действие, причина, итог проверки) по автоматическому факту."""
    if fact == "hit":
        action = "dispatch_crew" if _u(SOURCE, row.id, "action") < 0.5 else "remote_check"
        reason = ("confirmed_by_camera" if row.scenario == "guard_weekly"
                  else "confirmed_by_readings")
        return action, reason, "confirmed_event"
    if fact == "miss":
        return "reject", "false_alarm", "no_event"
    return "defer", "await_data", None


def emulate(db: Session, body: EmulateDecisionsIn) -> EmulateDecisionsOut:
    window = (models.Forecast.in_budget.is_(True), models.Forecast.asof >= body.date_from,
              models.Forecast.asof <= body.date_to)
    rows = db.execute(select(models.Forecast, models.Outcome)
                      .outerjoin(models.Outcome,
                                 models.Outcome.forecast_id == models.Forecast.id)
                      .where(*window).order_by(models.Forecast.id)).all()
    decisions: dict[str, list[models.Decision]] = {}
    for d in db.scalars(select(models.Decision)
                        .where(models.Decision.forecast_id.in_(
                            select(models.Forecast.id).where(*window)))
                        .order_by(models.Decision.id)):
        decisions.setdefault(d.forecast_id, []).append(d)

    out = dict.fromkeys(EmulateDecisionsOut.model_fields, 0)
    plans: dict[str, tuple[str, object]] = {}  # прогноз → (действие, время решения)
    for forecast, outcome in rows:
        mine = decisions.get(forecast.id, [])
        human_outcome = (outcome is not None and outcome.outcome_manual is not None
                         and outcome.source != SOURCE)
        if human_outcome or any(d.source != SOURCE for d in mine):
            out["skipped_live"] += 1
            continue
        fact = outcome.outcome_auto if outcome is not None else None
        out["with_fact"] += fact is not None
        want = (plan(forecast, fact)
                if fact is not None and selected(forecast.id, body.share) else None)

        kept = None
        for d in mine:
            if want is not None and kept is None and (d.action, d.reason_code) == want[:2]:
                kept = d
                continue
            db.delete(d)
            out["removed"] += 1
        decided_at = from_db(forecast.valid_from) + timedelta(
            hours=DECIDED_FROM_H, minutes=int(DECIDED_SPREAD_MIN * _u(SOURCE, forecast.id, "at")))
        if want is not None and kept is None:
            db.add(models.Decision(forecast_id=forecast.id, action=want[0],
                                   reason_code=want[1], comment=COMMENT, author=AUTHOR,
                                   created_at=to_db(decided_at), source=SOURCE))
            out["created"] += 1
        out["decisions"] += want is not None
        if want is not None:
            plans[forecast.id] = (want[0], decided_at)

        manual = want[2] if want is not None else None
        if manual is not None:
            if outcome.outcome_manual != manual or outcome.source != SOURCE:
                db.add(models.ManualOutcomeRevision(
                    forecast_id=forecast.id, outcome=manual, comment=COMMENT,
                    event_at=outcome.event_at, channel_id=outcome.channel_id,
                    author=AUTHOR, recorded_at=to_db(decided_at + timedelta(
                        hours=CHECKED_AFTER_H)), source=SOURCE))
            outcome.outcome_manual = manual
            outcome.comment = COMMENT
            outcome.author = AUTHOR
            outcome.updated_at = to_db(decided_at + timedelta(hours=CHECKED_AFTER_H))
            outcome.source = SOURCE
            out["outcomes"] += 1
        elif outcome is not None and outcome.source == SOURCE:
            # эмулированный итог прошлого засева больше не нужен: строка снова несёт
            # только автоматический факт с происхождением прогноза
            if outcome.outcome_manual is not None:
                db.add(models.ManualOutcomeRevision(
                    forecast_id=forecast.id, outcome=None, comment=None,
                    event_at=None, channel_id=None, author=AUTHOR,
                    recorded_at=to_db(decided_at + timedelta(hours=CHECKED_AFTER_H)),
                    source=SOURCE))
            outcome.outcome_manual = outcome.comment = outcome.author = None
            outcome.source = forecast.source
    window_ids = {forecast.id for forecast, _ in rows}
    out["work_orders"] = _emulate_orders(db, window_ids, plans)
    db.commit()
    return EmulateDecisionsOut(**out)


def _order_steps(forecast_ids: list, plans: dict) -> list[tuple[str, object]]:
    """(статус, время) по решениям прогнозов заявки; пусто — заявка остаётся черновиком."""
    decided = [plans.get(fid) for fid in forecast_ids]
    hits = [plan[1] for plan in decided if plan is not None and plan[0] in HIT_ACTIONS]
    if hits:
        start = min(hits)
        return [("confirmed", start), ("in_progress", start + timedelta(hours=IN_WORK_AFTER_H)),
                ("completed", start + timedelta(hours=CHECKED_AFTER_H))]
    if decided and all(plan is not None and plan[0] == "reject" for plan in decided):
        return [("cancelled", max(plan[1] for plan in decided))]
    return []


def _emulate_orders(db: Session, window_ids: set[str], plans: dict) -> int:
    """Статусы заявок окна по эмулированным решениям. Возвращает число сдвинутых заявок."""
    moved = 0
    for order in db.scalars(select(models.WorkOrder).order_by(models.WorkOrder.id)):
        if not window_ids.intersection(order.forecast_ids or []):
            continue
        history = list(db.scalars(select(models.WorkOrderHistory)
                                  .where(models.WorkOrderHistory.order_id == order.id)
                                  .order_by(models.WorkOrderHistory.id)))
        steps = [h for h in history if h.from_status is not None]
        if any(h.author != AUTHOR for h in steps):
            continue  # заявку двигал человек или учётная система
        for h in steps:
            db.delete(h)
        status = "draft"
        for to_status, at in _order_steps(order.forecast_ids or [], plans):
            db.add(models.WorkOrderHistory(order_id=order.id, from_status=status,
                                           to_status=to_status, author=AUTHOR,
                                           reason=ORDER_REASON[to_status], at=to_db(at)))
            status = to_status
        order.status = status
        moved += status != "draft"
    return moved
