"""Качество, уведомления, аудит, настройки, админские операции."""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

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
    base_rate: float | None = Field(
        default=None, description="Доля положительных среди кандидатов, из реестра метрик ML")
    rule_precision: float | None = Field(
        default=None, description="Точность простого правила при том же лимите; null, если "
                                  "в продукте само правило")
    reference_period: str | None = Field(
        default=None, description="Период, на котором посчитаны base_rate и rule_precision")
    reference_source: str | None = Field(
        default=None, description="Отчёт ML, из которого взяты base_rate и rule_precision")
    reference_note: str | None = Field(
        default=None, description="Как посчитаны base_rate и rule_precision")
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
    payload: dict | None = Field(default=None, description="Что изменилось, если обработчик "
                                                           "это записал: у PUT /settings/"
                                                           "parameters — поля «было, стало»")


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
    # Только недельная очередь guard_weekly, без голов A_link и D: так прелоад проходит
    # понедельники полугодия до окна дневных расчётов. asof — понедельник.
    weekly_only: bool = False

    @model_validator(mode="after")
    def _weekly_only_on_monday(self) -> "RunDailyIn":
        if self.weekly_only and self.asof.weekday() != 0:
            raise ValueError("weekly_only: недельная очередь считается по понедельникам")
        return self


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


class IssuedLogClearOut(BaseModel):
    date_from: date
    date_to: date
    deleted: int


class EmulateDecisionsIn(BaseModel):
    """Окно по asof прогноза, границы включены. share — доля прогнозов с фактом, которым
    достаётся эмулированное решение; остальные остаются нерешёнными."""
    date_from: date
    date_to: date
    share: float = Field(default=0.7, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> "EmulateDecisionsIn":
        if self.date_from > self.date_to:
            raise ValueError("date_from позже date_to")
        return self


class EmulateDecisionsOut(BaseModel):
    with_fact: int      # прогнозов окна в бюджете с автоматическим фактом
    decisions: int      # эмулированных решений после вызова
    outcomes: int       # эмулированных итогов проверки после вызова
    created: int        # решений добавлено этим вызовом
    removed: int        # эмулированных решений удалено этим вызовом
    skipped_live: int   # прогнозов с решением или итогом человека: не тронуты
    work_orders: int = 0  # заявок окна, которые эмуляция перевела из черновика
