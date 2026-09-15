from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    forecast_hours: int = 24

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=Path(os.getenv("MOSCOLLECTOR_DATA_DIR", "data/raw")),
            database_path=Path(os.getenv("MOSCOLLECTOR_DB_PATH", "data/moscollector.db")),
            forecast_hours=int(os.getenv("MOSCOLLECTOR_FORECAST_HOURS", "24")),
        )

