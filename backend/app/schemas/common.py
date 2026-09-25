"""Общие типы C2. Перечисления совпадают с кодами contracts/vocabularies.json (проверяет тест)."""
from typing import Literal

from pydantic import BaseModel

Scenario = Literal["sensor_link", "equipment_diag", "guard_weekly"]
Kind = Literal["alert", "weekly_recommendation"]
ScoreType = Literal["probability", "relative_priority"]
Source = Literal["live", "emulated", "stub"]
Action = Literal["dispatch_crew", "remote_check", "defer", "reject"]
ReasonCode = Literal[
    "false_alarm", "planned_works", "sensor_fault", "duplicate",
    "confirmed_by_readings", "confirmed_by_camera", "preventive",
    "no_crew", "await_data",
]
OutcomeManual = Literal["confirmed_event", "sensor_fault", "normal_activation", "no_event", "unknown"]
OutcomeAuto = Literal["hit", "miss", "unknown"]
ResultStatus = Literal["ok", "empty_valid", "no_data", "stale", "error"]
WorkOrderStatus = Literal["draft", "confirmed", "in_progress", "completed", "cancelled"]
Priority = Literal["urgent", "planned", "watch"]
EventClass = Literal["normal", "warning", "alarm", "critical", "fault", "service"]
Role = Literal["dispatcher", "technician", "analyst", "manager", "admin", "integration"]
DataStatus = Literal["ok", "no_data", "stale"]

class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int


class ErrorOut(BaseModel):
    detail: str


class ObjectRef(BaseModel):
    id: str | None = None
    name: str | None = None
    complex_id: str | None = None
    complex_name: str | None = None
    kind_ru: str | None = None


class ChannelRef(BaseModel):
    id: int
    name: str | None = None
    sensor_type: str | None = None
    picket_label: str | None = None
