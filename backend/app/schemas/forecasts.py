from datetime import date, datetime

from pydantic import BaseModel, Field

from .common import (
    Action,
    ChannelRef,
    DataStatus,
    Kind,
    ObjectRef,
    OutcomeAuto,
    OutcomeManual,
    ReasonCode,
    Scenario,
    ScoreType,
    Source,
)


class DecisionOut(BaseModel):
    id: int
    action: Action
    reason_code: ReasonCode
    comment: str | None = None
    author: str
    created_at: datetime
    source: Source


class ForecastItem(BaseModel):
    id: str
    kind: Kind
    scenario: Scenario
    scenario_title: str
    head: str
    asof: date
    valid_from: datetime
    valid_to: datetime
    horizon_hours: int
    score_type: ScoreType
    risk: float | None = None
    priority_score: float | None = None
    rank: int
    object: ObjectRef
    channel: ChannelRef | None = None
    data_status: DataStatus
    decision: DecisionOut | None = None
    outcome_auto: OutcomeAuto | None = None
    outcome_manual: OutcomeManual | None = None
    source: Source
    work_order_id: str | None = None
    case_key: str


class ForecastWeek(BaseModel):
    week_start: date = Field(description="Понедельник недели. Неделя — по asof, дате расчёта "
                                         "по МСК, с понедельника по воскресенье")
    issued: int
    hit: int
    miss: int
    unknown: int = Field(description="Исход unknown или ещё не определён (окно не закрыто) — "
                                     "как на экране «Качество»")
    decided: int = Field(description="Прогнозы с принятым решением диспетчера")


class ForecastSummary(BaseModel):
    weeks: list[ForecastWeek] = Field(
        description="Итог журнала по неделям при тех же фильтрах и группировке, что у "
                    "GET /forecasts, по всем строкам, а не по странице. По возрастанию "
                    "week_start; недели без строк не возвращаются")


class FactorItem(BaseModel):
    feature: str
    label: str
    contribution: float  # вклад в log-odds, показывать полосой без процентов


class VersionItem(BaseModel):
    run_id: int
    risk: float | None = None
    rank: int
    recorded_at: datetime


class DynamicsPoint(BaseModel):
    day: date
    alarms: int
    bad_states: int
    # Без этого счётчика alarms=0 не отличить: канал молчал (для потери связи это и есть
    # симптом) или сутки прошли без тревог.
    events: int = Field(description="Все события канала за сутки МСК в журнале сервиса. "
                                    "0 — сутки без событий: состояние неизвестно, "
                                    "alarms и bad_states за них не означают ноль")


class CalendarInfo(BaseModel):
    weekday: int
    weekday_title: str
    holiday: str | None = None


class ForecastCard(ForecastItem):
    factors: list[FactorItem] = Field(default_factory=list)
    maintenance_note: str | None = None
    evidence: str | None = None
    recent_alarm_days_7: int | None = None
    recent_alarm_days_30: int | None = None
    coverage_note: str | None = None
    versions: list[VersionItem] = Field(default_factory=list)
    decisions: list[DecisionOut] = Field(default_factory=list)
    dynamics_30d: list[DynamicsPoint] = Field(
        default_factory=list,
        description="30 суток МСК до asof включительно. Сутки без событий канала (events=0) "
                    "не пропускаются и не означают исправность: в журнале сервиса о них "
                    "нет данных. У прогноза без канала все events=0")
    calendar: CalendarInfo | None = None


class DecisionIn(BaseModel):
    action: Action
    reason_code: ReasonCode
    comment: str | None = Field(default=None, max_length=2000)


class OutcomeIn(BaseModel):
    outcome: OutcomeManual
    comment: str | None = Field(default=None, max_length=2000)
    event_at: datetime | None = None
    channel: int | None = None


class OutcomeOut(BaseModel):
    forecast_id: str
    outcome_auto: OutcomeAuto | None = None
    outcome_manual: OutcomeManual | None = None
    comment: str | None = None
    event_at: datetime | None = None
    channel: int | None = None
    author: str | None = None
    updated_at: datetime
    source: Source
