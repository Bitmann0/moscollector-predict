from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.common import Page, Priority, Scenario, WorkOrderStatus
from ..schemas.work_orders import (
    WorkOrderCard,
    WorkOrderCreate,
    WorkOrderItem,
    WorkOrderTransition,
)
from ..security import CurrentUser, require_perm
from ..services import work_orders

router = APIRouter(tags=["work-orders"])


@router.get("/work-orders", response_model=Page[WorkOrderItem],
            dependencies=[Depends(require_perm("view"))])
def list_orders(status: WorkOrderStatus | None = None, priority: Priority | None = None,
                scenario: Scenario | None = None, page: int = Query(1, ge=1),
                page_size: int = Query(50, ge=1, le=500),
                db: Session = Depends(get_db)) -> Page[WorkOrderItem]:
    return work_orders.list_orders(db, status=status, priority=priority, scenario=scenario,
                                   page=page, page_size=page_size)


@router.get("/work-orders/{order_id}", response_model=WorkOrderCard,
            dependencies=[Depends(require_perm("view"))])
def get_order(order_id: str, db: Session = Depends(get_db)) -> WorkOrderCard:
    card = work_orders.get(db, order_id)
    if card is None:
        raise HTTPException(status_code=404, detail="work_order_not_found")
    return card


@router.post("/work-orders", response_model=WorkOrderCard, status_code=201)
def create_order(body: WorkOrderCreate, db: Session = Depends(get_db),
                 user: CurrentUser = Depends(require_perm("work_order_manage"))) -> WorkOrderCard:
    return work_orders.create(db, body, user)


@router.patch("/work-orders/{order_id}", response_model=WorkOrderCard)
def transition(order_id: str, body: WorkOrderTransition, db: Session = Depends(get_db),
               user: CurrentUser = Depends(require_perm("work_order_manage",
                                                         "work_order_progress"))
               ) -> WorkOrderCard:
    # Право на конкретный переход (vocabularies.json: work_order_transition_perm)
    # проверяет сервис: оно зависит от целевого статуса.
    return work_orders.transition(db, order_id, body, user)
