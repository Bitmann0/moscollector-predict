from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.common import Page
from ..schemas.misc import AuditItem
from ..security import require_perm
from ..services import audit_query

router = APIRouter(tags=["audit"])


@router.get("/audit", response_model=Page[AuditItem],
            dependencies=[Depends(require_perm("admin"))])
def list_audit(user: str | None = None, date_from: date | None = Query(None, alias="from"),
               date_to: date | None = Query(None, alias="to"), page: int = Query(1, ge=1),
               page_size: int = Query(100, ge=1, le=1000),
               db: Session = Depends(get_db)) -> Page[AuditItem]:
    return audit_query.list_audit(db, user_login=user, date_from=date_from, date_to=date_to,
                                  page=page, page_size=page_size)
