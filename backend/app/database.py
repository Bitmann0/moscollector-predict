from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class MaintenanceRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS maintenance_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    sensor_name TEXT NOT NULL,
                    risk_score REAL NOT NULL,
                    priority TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    UNIQUE(channel_id, status)
                )
                """
            )

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM maintenance_requests ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def create(self, forecast: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM maintenance_requests WHERE channel_id = ? AND status = 'draft'",
                (forecast["channel_id"],),
            ).fetchone()
            if existing:
                return dict(existing), False
            cursor = connection.execute(
                """
                INSERT INTO maintenance_requests
                    (channel_id, sensor_name, risk_score, priority, recommendation, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    forecast["channel_id"],
                    forecast["sensor_name"],
                    forecast["risk_score"],
                    forecast["risk_level"],
                    forecast["recommendation"],
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM maintenance_requests WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row), True

