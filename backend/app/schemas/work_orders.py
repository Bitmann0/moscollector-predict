from datetime import datetime

from pydantic import BaseModel, Field

from .common import ChannelRef, ObjectRef, Priority, Scenario, Source, WorkOrderStatus


class WorkOrderItem(BaseModel):
    id: str
    scenario: Scenario
    object: ObjectRef
    priority: Priority
    work_type: str
    due_by: datetime
    status: WorkOrderStatus
    forecast_ids: list[str]
    created_by: str
    created_at: datetime
    source: Source


class HistoryItem(BaseModel):
    from_status: WorkOrderStatus | None = None
    to_status: WorkOrderStatus
    author: str
    reason: str | None = None
    at: datetime


class ChecklistOut(BaseModel):
    """Что проверить бригаде на оборудовании одного типа из заявки.

    Перечень есть только там, где его подтвердил заказчик: сейчас это фидеры, каналы
    «Состояние фазы» (services/phase_channels.py). basis называет, кто подтвердил.
    """
    equipment: str
    channels: list[ChannelRef]
    items: list[str]
    note: str | None = None
    basis: str


class WorkOrderCard(WorkOrderItem):
    rationale: list[str] = Field(default_factory=list)
    pickets: list[float] = Field(default_factory=list)
    channels: list[int] = Field(default_factory=list)
    checklist: list[ChecklistOut] = Field(default_factory=list)
    history: list[HistoryItem] = Field(default_factory=list)


class WorkOrderCreate(BaseModel):
    forecast_ids: list[str] = Field(min_length=1)


class WorkOrderTransition(BaseModel):
    expected_status: WorkOrderStatus
    status: WorkOrderStatus
    reason: str | None = Field(default=None, max_length=2000)
