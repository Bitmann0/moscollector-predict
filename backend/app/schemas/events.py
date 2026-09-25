from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import ChannelRef, EventClass, ObjectRef


class EventItem(BaseModel):
    """Строка журнала событий (форма Приложения 2 ТЗ)."""
    id: int
    ts: datetime                   # время регистрации
    object: ObjectRef
    channel: ChannelRef            # тип датчика — channel.sensor_type
    sensor_event: str | None = None  # событие датчика: значение_датчика как есть
    event_class: EventClass        # тип события
    event_class_title: str
    hint: str | None = None        # например «вероятно, плановая проверка»
    alarm: bool


class EventRowIn(BaseModel):
    """Строка журнала СМВУ в формате журнал_событий_пример.csv (C5)."""
    model_config = ConfigDict(populate_by_name=True)
    event_id: int = Field(alias="ид_события")
    channel_id: int = Field(alias="ид_канала_данных")
    day: str = Field(alias="дата")          # YYYY-MM-DD
    time: str = Field(alias="время")        # HH:MM:SS
    alarm: str | bool = Field(alias="тревожное")  # t/f/true/false
    value: str | None = Field(default=None, alias="значение_датчика")


class OdsRowIn(BaseModel):
    """Запись журнала ОДС (ТЗ §7) для /ingest/ods-journal."""
    ts: datetime
    obj_id: str | None = None
    record_type: str = Field(max_length=64)
    decision: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=2000)


class IngestBatchOut(BaseModel):
    batch_id: int
    kind: Literal["smvu", "ods", "reset"]
    received_at: datetime
    rows_total: int
    accepted: int
    duplicates: int
    rejected: int
    outside_demo_window: int
    status: Literal["accepted", "partial", "rejected"]


class ResetDayOut(BaseModel):
    day: date
    deleted_events: int
