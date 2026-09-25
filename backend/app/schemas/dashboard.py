from datetime import date

from pydantic import BaseModel

from .common import Scenario, Source
from .system import HeadState


class ScenarioKpi(BaseModel):
    scenario: Scenario
    title: str
    open_forecasts: int
    coverage_fraction: float | None = None


class SeriesPoint(BaseModel):
    day: date
    value: float


class DashboardSummary(BaseModel):
    demo_today: date
    scenarios: list[ScenarioKpi]
    work_orders_by_status: dict[str, int]
    alarms_24h: int
    planned_like_alarms_24h: int  # «сработки, похожие на плановые работы» (C5)
    heads: list[HeadState]
    series_forecasts_per_day: list[SeriesPoint]
    series_coverage_per_day: list[SeriesPoint]
    source: Source
