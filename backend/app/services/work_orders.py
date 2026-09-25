"""Сигнатура сервиса (владелец BE-06). Тело — задача 3 плана каркаса, см. signatures.md."""
from sqlalchemy.orm import Session

from ..schemas.common import Page
from ..schemas.work_orders import (
    WorkOrderCard,
    WorkOrderCreate,
    WorkOrderItem,
    WorkOrderTransition,
)
from ..security import CurrentUser


def list_orders(db: Session, *, status: str | None, priority: str | None,
                scenario: str | None, page: int, page_size: int) -> Page[WorkOrderItem]:
    raise NotImplementedError("каркас: задача 3")


def get(db: Session, order_id: str) -> WorkOrderCard | None:
    raise NotImplementedError("каркас: задача 3")


def create(db: Session, body: WorkOrderCreate, user: CurrentUser) -> WorkOrderCard:
    raise NotImplementedError("каркас: задача 3")


def transition(db: Session, order_id: str, body: WorkOrderTransition,
               user: CurrentUser) -> WorkOrderCard:
    raise NotImplementedError("каркас: задача 3")
