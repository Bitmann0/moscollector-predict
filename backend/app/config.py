from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    forecast_hours: int = 24
    mode: str = "historical"
    max_data_age_hours: float = 6
    time_zone: str = "Europe/Moscow"
    analysis_window_hours: int = 24

    def __post_init__(self) -> None:
        if self.mode not in {"historical", "live"}:
            raise ValueError("MOSCOLLECTOR_MODE: historical или live")
        if self.max_data_age_hours <= 0 or self.analysis_window_hours <= 0:
            raise ValueError("Допустимый возраст данных и окно анализа должны быть положительными")
        if self.forecast_hours < 24:
            raise ValueError("Целевой горизонт будущей модели должен быть не меньше 24 часов")
        ZoneInfo(self.time_zone)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=Path(os.getenv("MOSCOLLECTOR_DATA_DIR", "data/raw")),
            database_path=Path(os.getenv("MOSCOLLECTOR_DB_PATH", "data/moscollector.db")),
            forecast_hours=int(os.getenv("MOSCOLLECTOR_FORECAST_HOURS", "24")),
            mode=os.getenv("MOSCOLLECTOR_MODE", "historical"),
            max_data_age_hours=float(os.getenv("MOSCOLLECTOR_MAX_DATA_AGE_HOURS", "6")),
            time_zone=os.getenv("MOSCOLLECTOR_TIME_ZONE", "Europe/Moscow"),
            analysis_window_hours=int(os.getenv("MOSCOLLECTOR_ANALYSIS_WINDOW_HOURS", "24")),
        )
