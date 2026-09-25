from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.system import SystemStatus
from ..security import require_perm
from ..services import system
from ..services.ml_client import MlClient, get_ml_client

router = APIRouter(tags=["system"])


@router.get("/system/status", response_model=SystemStatus,
            dependencies=[Depends(require_perm("view"))])
def status(db: Session = Depends(get_db), ml: MlClient = Depends(get_ml_client)) -> SystemStatus:
    return system.status(db, ml)
