from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.misc import RunDailyIn, RunDailyOut
from ..security import require_perm
from ..services import daily_run
from ..services.ml_client import MlClient, get_ml_client

router = APIRouter(tags=["admin"])


@router.post("/admin/run-daily", response_model=RunDailyOut,
             dependencies=[Depends(require_perm("admin"))])
def run_daily(body: RunDailyIn, db: Session = Depends(get_db),
              ml: MlClient = Depends(get_ml_client)) -> RunDailyOut:
    return daily_run.run_daily(db, body.asof, ml)
