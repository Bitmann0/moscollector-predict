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

        manual = want[2] if want is not None else None
        if manual is not None:
            outcome.outcome_manual = manual
            outcome.comment = COMMENT
            outcome.author = AUTHOR
            outcome.updated_at = to_db(decided_at + timedelta(hours=CHECKED_AFTER_H))
            outcome.source = SOURCE
            out["outcomes"] += 1
        elif outcome is not None and outcome.source == SOURCE:
            # эмулированный итог прошлого засева больше не нужен: строка снова несёт
            # только автоматический факт с происхождением прогноза
            outcome.outcome_manual = outcome.comment = outcome.author = None
            outcome.source = forecast.source
    db.commit()
    return EmulateDecisionsOut(**out)
