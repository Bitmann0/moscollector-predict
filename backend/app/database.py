from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class MaintenanceRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS forecast_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    author TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    forecast_snapshot TEXT NOT NULL
                )
            """)
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
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(maintenance_requests)")
            }
            if "assessment_mode" not in columns:
                connection.execute(
                    "ALTER TABLE maintenance_requests ADD COLUMN assessment_mode TEXT "
                    "NOT NULL DEFAULT 'legacy'"
                )
            if "data_as_of" not in columns:
                connection.execute("ALTER TABLE maintenance_requests ADD COLUMN data_as_of TEXT")

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM maintenance_requests ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def create(self, forecast: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO maintenance_requests
                    (channel_id, sensor_name, risk_score, priority, recommendation, created_at,
                     assessment_mode, data_as_of)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, status) DO NOTHING
                """,
                (
                    forecast["channel_id"],
                    forecast["sensor_name"],
                    forecast["risk_score"],
                    forecast["risk_level"],
                    forecast["recommendation"],
                    now,
                    forecast.get("assessment_mode", "legacy"),
                    forecast.get("data_as_of"),
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM maintenance_requests WHERE channel_id = ? AND status = 'draft'",
                (forecast["channel_id"],),
            ).fetchone()
            if row["assessment_mode"] != forecast.get("assessment_mode", "legacy"):
                raise ValueError("Для канала есть черновик другого режима; проверьте журнал")
        return dict(row), created

    def feedback(self, channel_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM forecast_feedback WHERE channel_id = ? ORDER BY id DESC",
                (channel_id,),
            ).fetchall()
        return [
            {**dict(row), "forecast_snapshot": json.loads(row["forecast_snapshot"])} for row in rows
        ]

    def add_feedback(
        self, forecast: dict[str, Any], decision: str, reason: str, author: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO forecast_feedback
                (channel_id, decision, reason, author, created_at, forecast_snapshot)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    forecast["channel_id"],
                    decision,
                    reason,
                    author,
                    datetime.now(UTC).isoformat(),
                    json.dumps(forecast, ensure_ascii=False),
                ),
            )
            feedback_id = cursor.lastrowid
        return next(
            item for item in self.feedback(forecast["channel_id"]) if item["id"] == feedback_id
        )
