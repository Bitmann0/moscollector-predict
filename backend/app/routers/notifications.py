from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.common import Page
from ..schemas.misc import NotificationItem
from ..security import CurrentUser, require_perm
from ..services import notifications

router = APIRouter(tags=["notifications"])


@router.get("/notifications", response_model=Page[NotificationItem])
def list_notifications(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                       db: Session = Depends(get_db),
                       user: CurrentUser = Depends(require_perm("view"))
                       ) -> Page[NotificationItem]:
    return notifications.list_notifications(db, user, page, page_size)


@router.post("/notifications/{notification_id}/read", status_code=204)
def mark_read(notification_id: int, db: Session = Depends(get_db),
              user: CurrentUser = Depends(require_perm("view"))) -> Response:
    notifications.mark_read(db, notification_id, user)
    return Response(status_code=204)
