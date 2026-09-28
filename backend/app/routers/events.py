from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.common import EventClass, IncidentGroup, Page
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
                hide_normal_gas: bool = Query(
                    False, description="Скрыть события класса normal у газовых датчиков"),
                incident_group: IncidentGroup | None = Query(
                    None, description="Группа аварии: пожар, наводнение, газ, проникновение, "
                                      "аномальная температура"),
                sort: events.Sort = Query(
                    "ts", description="Колонка: время, объект, тип датчика, событие датчика, "
                                      "тип события. Кроме ts — только при from и to не шире "
                                      f"{events.SORT_SPAN_DAYS} суток, иначе 422 sort_needs_range"),
                order: events.Order = Query("desc"),
                db: Session = Depends(get_db)) -> Page[EventItem]:
    if not events.sort_range_ok(sort, date_from, date_to):
        raise HTTPException(status_code=422, detail="sort_needs_range")
    return events.list_events(db, date_from=date_from, date_to=date_to, obj=obj,
                              sensor_type=sensor_type, event_class=event_class, q=q,
                              page=page, page_size=page_size,
                              hide_normal_gas=hide_normal_gas,
                              incident_group=incident_group, sort=sort, order=order)
