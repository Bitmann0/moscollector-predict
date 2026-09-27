from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.misc import (
    EmulateDecisionsIn,
    EmulateDecisionsOut,
    IssuedLogClearOut,
    RunDailyIn,
    RunDailyOut,
)
from ..security import require_perm
from ..services import daily_run, emulation
from ..services.ml_client import MlClient, get_ml_client

router = APIRouter(tags=["admin"])


@router.post("/admin/run-daily", response_model=RunDailyOut,
             dependencies=[Depends(require_perm("admin"))])
def run_daily(body: RunDailyIn, db: Session = Depends(get_db),
              ml: MlClient = Depends(get_ml_client)) -> RunDailyOut:
    return daily_run.run_daily(db, body.asof, ml, weekly_only=body.weekly_only)


@router.delete("/admin/issued-log", response_model=IssuedLogClearOut,
               dependencies=[Depends(require_perm("admin"))])
def clear_issued_log(date_from: date = Query(alias="from"), date_to: date = Query(alias="to"),
                     db: Session = Depends(get_db)) -> IssuedLogClearOut:
    """Журнал выданного за asof из [from, to]: прелоад чистит окно перед прогоном."""
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="from позже to")
    deleted = daily_run.clear_issued_log(db, date_from, date_to)
    return IssuedLogClearOut(date_from=date_from, date_to=date_to, deleted=deleted)


@router.post("/admin/emulate-decisions", response_model=EmulateDecisionsOut,
             dependencies=[Depends(require_perm("admin"))])
def emulate_decisions(body: EmulateDecisionsIn,
                      db: Session = Depends(get_db)) -> EmulateDecisionsOut:
    """Эмулированные решения и итоги проверки (source=emulated) по факту прогнозов окна."""
    return emulation.emulate(db, body)
