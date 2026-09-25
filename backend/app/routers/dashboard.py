from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.dashboard import DashboardSummary
from ..security import require_perm
from ..services import dashboard

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary", response_model=DashboardSummary,
            dependencies=[Depends(require_perm("view"))])
def summary(db: Session = Depends(get_db)) -> DashboardSummary:
    return dashboard.summary(db)
