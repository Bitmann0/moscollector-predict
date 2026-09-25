"""Качество, уведомления, аудит, настройки, админские операции."""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .common import ResultStatus, Scenario, Source


class QualityWeek(BaseModel):
    week_start: date
    issued: int
    hit: int
    miss: int
    unknown: int
    precision: float | None = None


class QualityOut(BaseModel):
    scenario: Scenario
    weeks: list[QualityWeek]
    base_rate: float | None = None
    rule_precision: float | None = None
    note: str | None = None
    source: Source


NotificationKind = Literal["alert.new", "event.alarm", "run.finished", "workorder.changed"]


class NotificationItem(BaseModel):
    id: int
    ts: datetime
    kind: NotificationKind
    severity: Literal["info", "warning", "critical"]
    title: str
    payload: dict = Field(default_factory=dict)
    read: bool = False


class AuditItem(BaseModel):
    id: int
    ts: datetime
    user_login: str | None = None
    role: str | None = None
    method: str
    path: str
    status: int
    entity: str | None = None


class SettingsOut(BaseModel):
    demo_today: date
    mode: Literal["archive", "replay"]
    replay_speed: int
    locked: bool


class SettingsIn(BaseModel):
    demo_today: date | None = None
    mode: Literal["archive", "replay"] | None = None
    replay_speed: int | None = Field(default=None, ge=1, le=3600)


class RunDailyIn(BaseModel):
    asof: date


class HeadRunResult(BaseModel):
    result_status: ResultStatus
    alerts_in_budget: int
    detail: str | None = None


class RunDailyOut(BaseModel):
    run_id: int
    asof: date
    heads: dict[str, HeadRunResult]
    forecasts_upserted: int
    work_orders_upserted: int
