from __future__ import annotations

import json
import sqlite3
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
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
            # Migrate the original per-status uniqueness constraint. Closed requests
            # must not prevent a later maintenance cycle for the same channel.
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'maintenance_requests'"
            ).fetchone()[0]
            if "UNIQUE(channel_id, status)" in schema:
                connection.execute("ALTER TABLE maintenance_requests RENAME TO requests_legacy")
                connection.execute(schema.replace(
                    ",\n                    UNIQUE(channel_id, status)", ""
                ))
                connection.execute(
                    "INSERT INTO maintenance_requests SELECT * FROM requests_legacy"
                )
                connection.execute("DROP TABLE requests_legacy")
            connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_request
                ON maintenance_requests(channel_id) WHERE status IN ('draft', 'in_progress')""")
            connection.execute("""CREATE TABLE IF NOT EXISTS maintenance_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                previous_status TEXT NOT NULL,
                status TEXT NOT NULL,
                author TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM maintenance_requests ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def create(self, forecast: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM maintenance_requests WHERE channel_id = ? "
                "AND status IN ('draft', 'in_progress')",
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

    def request_detail(self, request_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM maintenance_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            history = connection.execute(
                "SELECT * FROM maintenance_history WHERE request_id = ? ORDER BY id",
                (request_id,),
            ).fetchall()
        return {**dict(row), "history": [dict(item) for item in history]}

    def transition(self, request_id: int, expected_status: str, status: str,
                   author: str, reason: str) -> dict[str, Any]:
        allowed = {"draft": {"in_progress", "cancelled"},
                   "in_progress": {"completed", "cancelled"}}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM maintenance_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            previous = row["status"]
            if previous != expected_status:
                raise ValueError("Заявка уже изменена. Обновите данные.")
            if status not in allowed.get(previous, set()):
                raise ValueError("Недопустимый переход статуса заявки")
            connection.execute(
                "UPDATE maintenance_requests SET status = ? WHERE id = ?", (status, request_id)
            )
            connection.execute("""INSERT INTO maintenance_history
                (request_id, previous_status, status, author, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (request_id, previous, status, author, reason, datetime.now(UTC).isoformat()))
        return self.request_detail(request_id)

    def feedback(self, channel_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM forecast_feedback WHERE channel_id = ? ORDER BY id DESC",
                (channel_id,),
            ).fetchall()
        return [{**dict(row), "forecast_snapshot": json.loads(row["forecast_snapshot"])}
                for row in rows]

    def add_feedback(self, forecast: dict[str, Any], decision: str,
                     reason: str, author: str) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO forecast_feedback
                (channel_id, decision, reason, author, created_at, forecast_snapshot)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (forecast["channel_id"], decision, reason, author,
                 datetime.now(UTC).isoformat(), json.dumps(forecast, ensure_ascii=False)),
            )
            feedback_id = cursor.lastrowid
        return next(item for item in self.feedback(forecast["channel_id"])
                    if item["id"] == feedback_id)
