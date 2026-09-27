"""Контракт C1 «ML → backend» в виде pydantic-моделей.

Это HTTP-обёртка над датаклассами mkl.contract и ответом guard_weekly: поля
совпадают с Alert.to_dict(), Coverage.to_dict(), WorkOrder и weekly_inspections().
Модуль не импортирует тяжёлые части mkl, поэтому сервис в режиме заглушки
стартует без данных и моделей.

Источник истины для backend: схема экспортируется в contracts/ml_v1.schema.json
скриптом scripts/export_contracts.py, а backend сверяет с ней своё зеркало
(backend/app/schemas/ml.py). Меняешь поле здесь — перегенерируй контракты.
"""
import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

Mode = Literal["stub", "real"]
Source = Literal["stub", "live"]
PilotHead = Literal["A_link", "D"]
ResultStatus = Literal["ok", "empty_valid", "no_data", "stale", "error"]
ReadyStatus = Literal["ready", "missing_data", "stale", "stale_source", "future_source", "error"]
AlertStatus = Literal["ok", "no_data", "stale"]
Outcome = Literal["hit", "miss", "unknown"]
MlPriority = Literal["срочная", "плановая", "наблюдение"]


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    mode: Mode
    schema_version: str = SCHEMA_VERSION


class ReadyResponse(BaseModel):
    status: ReadyStatus
    asof: dt.date | None = None
    data_last_day: dt.date | None = None
    detail: str | None = None
    source: Source


class DirectionHead(BaseModel):
    head: str
    title: str
    horizon_hours: int
    budget_per_day: int | None = None


class DirectionItem(BaseModel):
    direction: str
    title: str
    heads: list[DirectionHead]


class IssuedEntry(BaseModel):
    """Строка журнала выданного: канал (A_link, D) или объект — и день выдачи."""
    channel: int | None = None
    obj: str | None = None
    sent_day: dt.date


class ScoreRequest(BaseModel):
    asof: dt.date
    heads: list[PilotHead] = Field(default_factory=lambda: ["A_link", "D"])
    issued_histories: dict[PilotHead, list[IssuedEntry]] = Field(default_factory=dict)
    history_complete_from: dt.date | None = None
    with_factors: bool = True


class HeadStatus(BaseModel):
    result_status: ResultStatus
    model_version: str | None = None
    threshold_end: dt.date | None = None
    model_lag_days: int | None = None
    threshold_feasible: bool | None = None
    detail: str | None = None


class AddressOut(BaseModel):
    obj: str | None = None
    obj_parent: str | None = None
    obj_kind: str | None = None
    channel: int | None = None
    segment: int | None = None
    picket: float | None = None
    obj_name: str | None = None
    obj_parent_name: str | None = None
    obj_kind_ru: str | None = None
    sensor_name: str | None = None
    sensor_type: str | None = None
    tag: str | None = None
    picket_label: str | None = None
    segment_label: str | None = None
    address_known: bool = True


class FactorOut(BaseModel):
    feature: str
    label: str
    contribution: float  # вклад в log-odds некалиброванной модели, не в вероятность


class AlertOut(BaseModel):
    alert_id: str
    case_key: str
    schema_version: str = SCHEMA_VERSION
    head: str
    direction: str
    direction_title: str
    title: str
    asof: dt.date
    valid_from: dt.datetime  # без таймзоны: Europe/Moscow
    valid_to: dt.datetime
    horizon_hours: int
    risk: float  # вероятность у модели; у головы-правила (D) — значение признака
    rank: int
    in_budget: bool
    above_threshold: bool | None = None
    address: AddressOut
    status: AlertStatus = "ok"
    status_note: str | None = None
    model_version: str | None = None
    feature_signature: str | None = None
    factors: list[FactorOut] = Field(default_factory=list)


class CoverageOut(BaseModel):
    head: str
    direction: str
    entities_total: int
    entities_scored: int
    reason: str | None = None
    fraction: float


class WorkOrderOut(BaseModel):
    order_id: str
    schema_version: str = SCHEMA_VERSION
    created_for: dt.date
    due_by: dt.datetime
    priority: MlPriority
    work_type: str
    direction: str
    direction_title: str
    obj: str | None = None
    obj_parent: str | None = None
    obj_kind: str | None = None
    obj_name: str | None = None
    obj_parent_name: str | None = None
    pickets: list[float] = Field(default_factory=list)
    alert_ids: list[str] = Field(default_factory=list)
    case_keys: list[str] = Field(default_factory=list)
    n_alerts: int = 0
    max_risk: float = 0.0
    channels: list[int] = Field(default_factory=list)
    rationale: list[str] = Field(default_factory=list)
    status: str = "новая"


class ScoreResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    asof: dt.date
    source: Source
    heads: dict[str, HeadStatus]
    data_snapshot: dict = Field(default_factory=dict)
    alerts: list[AlertOut] = Field(default_factory=list)
    coverage: list[CoverageOut] = Field(default_factory=list)
    work_orders: list[WorkOrderOut] = Field(default_factory=list)


class WeeklyPriority(BaseModel):
    obj: str
    rank: int
    recommendation_id: str
    case_key: str
    priority_score: float
    recent_alarm_days_7: int
    recent_alarm_days_30: int
    guard_state_age_days: int | None = None
    evidence: str
    obj_name: str | None = None
    obj_parent_name: str | None = None
    address_known: bool = False


class WeeklyResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    asof: dt.date
    valid_from: dt.date           # D+2
    valid_to: dt.date             # D+9, граница не входит в окно
    next_run: dt.date
    target: str = "continued_recorded_smvu_guard_alarm_activity"
    target_version: int = 2
    model_version: str = "weekly_recurrence_rule_v1"
    method: str = "weekly_recurrence_rule_v1"
    action: str = "manual_plan_guard_loop_inspection"
    score_type: Literal["relative_priority_not_probability"] = "relative_priority_not_probability"
    result_status: ResultStatus
    policy: dict = Field(default_factory=dict)
    priorities: list[WeeklyPriority] = Field(default_factory=list)
    event_cache_through: dt.date | None = None
    data_snapshot: dict = Field(default_factory=dict)
    data_last_day: dt.date | None = None
    source: Source


class OutcomeQuery(BaseModel):
    id: str
    kind: Literal["alert", "weekly_recommendation"]
    head: str
    channel: int | None = None
    obj: str | None = None
    asof: dt.date


class OutcomeResult(BaseModel):
    id: str
    outcome: Outcome
