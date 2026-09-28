"""Настраиваемые параметры продукта (ТЗ §18, ML2-13): тело и ответ /settings/parameters.

Проверенные значения — те, на которых посчитаны замеры в комментариях
backend/app/services/semantics.py и точность голов в ml/reports. Диапазоны отсекают
опечатку, а не задают норматив: 50 вместо 5,0 у метана или 0 извещателей у серии.
Лимит рекомендаций выше проверенного не принимается: точность при нём не считалась.
"""
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from .common import IncidentGroup, Scenario

Weekday = Annotated[int, Field(ge=0, le=6, description="0 — понедельник, 6 — воскресенье")]
# Предупреждение и неисправность уведомлений не создают и здесь не включаются:
# предупреждение — 74,2 % записей «Обнаружен дым» (docstring semantics.py).
NotifyClass = Literal["alarm", "critical"]

# Проверенный лимит показа по сценарию, в сутки (недельная очередь — в неделю):
# sensor_link — budget_per_day: 20 у A_link в ml/configs/heads.yaml, product_policy
# max_20_per_day; equipment_diag — budget_per_day: 3 у D там же; guard_weekly —
# BUDGET = 4 в ml/src/mkl/guard_weekly.py. Сверку держит tests/test_parameters.py.
VERIFIED_LIMITS: dict[str, int] = {"sensor_link": 20, "equipment_diag": 3, "guard_weekly": 4}
RECLASSIFY_MAX_DAYS = 31


def _unique_sorted(values: list, name: str) -> list:
    if len(set(values)) != len(values):
        raise ValueError(f"{name}: значения повторяются")
    return sorted(values)


class GasThresholds(BaseModel):
    """Метан по доле объёма: от alarm_pct — тревога, от critical_pct — критическое."""
    alarm_pct: float = Field(ge=0.1, le=5.0, description="Тревога от этой доли метана, % объёма")
    critical_pct: float = Field(ge=0.2, le=10.0,
                                description="Критическое от этой доли метана, % объёма")

    @model_validator(mode="after")
    def _ordered(self) -> "GasThresholds":
        if self.alarm_pct >= self.critical_pct:
            raise ValueError("порог тревоги должен быть ниже критического")
        return self


class TimeWindow(BaseModel):
    """Часы МСК [hour_from, hour_to) по дням недели: 9 и 15 — с 9:00 до 14:59."""
    hour_from: int = Field(ge=0, le=23, description="Первый час окна, МСК")
    hour_to: int = Field(ge=1, le=24, description="Час, с которого окно закрыто: 15 — до 14:59")
    days: list[Weekday] = Field(min_length=1, max_length=7)

    @field_validator("days")
    @classmethod
    def _days(cls, value: list[int]) -> list[int]:
        return _unique_sorted(value, "days")

    @model_validator(mode="after")
    def _ordered(self) -> "TimeWindow":
        if self.hour_from >= self.hour_to:
            raise ValueError("окно должно начинаться раньше, чем кончается")
        return self


class SeriesParams(BaseModel):
    """Серия ППР/ТО: не меньше N разных извещателей за окно, первое — в рабочее время."""
    window_min: int = Field(ge=1, le=60, description="Окно серии, минут")
    fire_min: int = Field(ge=2, le=50, description="Пожарных извещателей одного объекта")
    gas_min: int = Field(ge=2, le=50, description="Газоанализаторов одного комплекса")
    work: TimeWindow


class NotifyParams(BaseModel):
    """event.alarm создаёт событие класса из classes. Событие с группой аварии — ещё и
    только если группа в groups; событие без группы уведомляет по классу."""
    classes: list[NotifyClass]
    groups: list[IncidentGroup]

    @field_validator("classes", "groups")
    @classmethod
    def _unique(cls, value: list[str], info) -> list[str]:
        return _unique_sorted(value, info.field_name)


class LimitParams(BaseModel):
    """Сколько рекомендаций ML в бюджете показывать и уведомлять: первые N по рангу."""
    sensor_link: int = Field(ge=1, description="В сутки; проверено 20")
    equipment_diag: int = Field(ge=1, description="В сутки; проверено 3")
    guard_weekly: int = Field(ge=1, description="В неделю; проверено 4")

    @field_validator("sensor_link", "equipment_diag", "guard_weekly")
    @classmethod
    def _verified(cls, value: int, info) -> int:
        verified = VERIFIED_LIMITS[info.field_name]
        if value > verified:
            raise PydanticCustomError(
                "limit_above_verified",
                "Лимит {value} больше проверенного {verified}: точность прогноза при таком "
                "лимите не проверялась. Допустимо от 1 до {verified}",
                {"value": value, "verified": verified})
        return value


class Parameters(BaseModel):
    gas: GasThresholds
    gas_window: TimeWindow = Field(description="«Обнаружен газ» в эти часы — подсказка "
                                               "«вероятно, ППР или ТО»")
    series: SeriesParams
    notify: NotifyParams
    limits: LimitParams


class ParametersIn(BaseModel):
    expected_version: int = Field(ge=0, description="version из последнего GET; другой — 409")
    values: Parameters


class Bound(BaseModel):
    min: float
    max: float


class ParametersOut(BaseModel):
    values: Parameters
    verified: Parameters
    bounds: dict[str, Bound] = Field(description="Допустимый диапазон поля: ключ — путь "
                                                 "поля через точку, например gas.alarm_pct")
    locked: bool
    version: int = Field(description="0 — параметры не менялись, действуют проверенные")
    updated_at: datetime | None = None
    updated_by: str | None = None


class ReclassifyIn(BaseModel):
    """Сутки МСК, границы включены, не больше RECLASSIFY_MAX_DAYS суток."""
    date_from: date
    date_to: date

    @model_validator(mode="after")
    def _span(self) -> "ReclassifyIn":
        if self.date_from > self.date_to:
            raise ValueError("date_from позже date_to")
        if (self.date_to - self.date_from).days + 1 > RECLASSIFY_MAX_DAYS:
            raise ValueError(f"не больше {RECLASSIFY_MAX_DAYS} суток за вызов; всю историю "
                             "пересчитывает scripts/reclassify_events.py")
        return self


class ClassChange(BaseModel):
    old: str | None
    new: str
    count: int


class ReclassifyOut(BaseModel):
    date_from: date
    date_to: date
    rows: int
    changed: int
    classes: list[ClassChange]
    hints_changed: int
    groups_changed: int
    seconds: float


LIMIT_SCENARIOS: tuple[Scenario, ...] = ("sensor_link", "equipment_diag", "guard_weekly")
