"""Состояние системы для шапки интерфейса: демо-день, ML, статус сценариев. Живое.

Владелец BE-05. Статус головы берётся из последнего прогона, в котором голова
считалась: недельная очередь считается только по понедельникам, и во вторник её
статус не должен пропадать из шапки.
"""
from datetime import date

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, vocab
from ..config import get_settings
from ..schemas.system import HeadState, LastRun, MlReady, SystemStatus
from . import settings_store
from .helpers import from_db
from .ml_client import MlClient, MlUnavailable

RUNS_LOOKBACK = 60  # прогонов назад, где ищем статус головы: два месяца дневных запусков

ML_MODE_BY_SOURCE = {"stub": "stub", "live": "real"}


def head_states(db: Session) -> list[HeadState]:
    scenarios = vocab.load()["scenario"]
    found: dict[str, dict] = {}
    runs = db.scalars(select(models.ForecastRun).order_by(models.ForecastRun.id.desc())
                      .limit(RUNS_LOOKBACK))
    for run in runs:
        for head, state in (run.heads or {}).items():
            found.setdefault(head, state)
        if all(s["head"] in found for s in scenarios):
            break
    out = []
    for s in scenarios:
        state = found.get(s["head"]) or {}
        out.append(HeadState(scenario=s["code"], head=s["head"],
                             result_status=state.get("result_status"),
                             model_lag_days=state.get("model_lag_days"),
                             threshold_feasible=state.get("threshold_feasible"),
                             detail=state.get("detail")))
    return out


def ml_ready(ml: MlClient, asof: date) -> MlReady:
    """Готовность ML на демо-день. Без asof ML сверяет данные с настоящей датой, а
    данные кончаются 2026-06-30, поэтому ответ всегда stale_source (раздел 1 плана)."""
    try:
        ready = ml.ready(asof)
    except (MlUnavailable, ValidationError):
        return MlReady(reachable=False)
    return MlReady(reachable=True, status=ready.status,
                   mode=ML_MODE_BY_SOURCE.get(ready.source, ready.source),
                   data_last_day=ready.data_last_day)


def last_run(db: Session) -> LastRun | None:
    run = db.scalars(select(models.ForecastRun).order_by(models.ForecastRun.id.desc())
                     .limit(1)).first()
    if run is None:
        return None
    return LastRun(run_id=run.id, asof=run.asof, kind=run.kind,
                   finished_at=from_db(run.finished_at))


def status(db: Session, ml: MlClient) -> SystemStatus:
    demo = settings_store.get(db)
    return SystemStatus(demo_today=demo.demo_today, mode=demo.mode,
                        settings_locked=get_settings().demo_settings_locked,
                        ml=ml_ready(ml, demo.demo_today), heads=head_states(db),
                        last_run=last_run(db))
