from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from .common import ResultStatus, Scenario


class HeadState(BaseModel):
    """Статус сценария по последнему прогону: FE-10 показывает его отдельным состоянием."""
    scenario: Scenario
    head: str
    result_status: ResultStatus | None = None
    model_lag_days: int | None = None
    threshold_feasible: bool | None = None
    detail: str | None = None


class MlReady(BaseModel):
    reachable: bool
    status: str | None = None
    mode: str | None = None
    data_last_day: date | None = None


class LastRun(BaseModel):
    run_id: int
    asof: date
    kind: str
    finished_at: datetime | None = None


class SystemStatus(BaseModel):
    demo_today: date
    mode: Literal["archive", "replay"]
    settings_locked: bool
    ml: MlReady
    heads: list[HeadState]
    last_run: LastRun | None = None
