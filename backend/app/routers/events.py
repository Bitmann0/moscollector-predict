from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.common import EventClass, Page
from ..schemas.events import EventItem
from ..security import require_perm
from ..services import events

router = APIRouter(tags=["events"])


@router.get("/events", response_model=Page[EventItem],
            dependencies=[Depends(require_perm("view"))])
def list_events(date_from: date | None = Query(None, alias="from"),
                date_to: date | None = Query(None, alias="to"), obj: str | None = None,
                sensor_type: str | None = None, event_class: EventClass | None = None,
                q: str | None = None, page: int = Query(1, ge=1),
                page_size: int = Query(100, ge=1, le=1000),
                db: Session = Depends(get_db)) -> Page[EventItem]:
    return events.list_events(db, date_from=date_from, date_to=date_to, obj=obj,
                              sensor_type=sensor_type, event_class=event_class, q=q,
                              page=page, page_size=page_size)
