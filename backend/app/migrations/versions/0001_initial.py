"""Все таблицы BE-02 одной миграцией.

Написана вручную по app/models.py. Типы переносимые (JSON, DateTime(timezone=True)),
поэтому одна и та же миграция идёт на SQLite (тесты) и PostgreSQL 16 (compose, CI).
Совпадение со схемой моделей проверяет tests/test_migration.py.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-25
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# BIGINT с автоинкрементом; в SQLite автоинкремент есть только у INTEGER PRIMARY KEY.
BigIntPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
TS = sa.DateTime(timezone=True)


def _id() -> sa.Column:
    return sa.Column("id", BigIntPK, primary_key=True, autoincrement=True)


def _index(table: str, *columns: str) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("login", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("password_hash", sa.String(300), nullable=False),
    )

    op.create_table(
        "ref_objects",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.String(32), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
    )
    _index("ref_objects", "parent_id")

    op.create_table(
        "ref_channels",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("obj_id", sa.String(32), nullable=False),
        sa.Column("system", sa.String(100), nullable=True),
        sa.Column("sensor_type", sa.String(100), nullable=True),
        sa.Column("tag", sa.String(100), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("picket", sa.Float(), nullable=True),
    )
    _index("ref_channels", "obj_id")

    op.create_table(
        "forecast_runs",
        _id(),
        sa.Column("asof", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("heads", sa.JSON(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=True),
    )
    _index("forecast_runs", "asof")

    op.create_table(
        "forecasts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("scenario", sa.String(32), nullable=False),
        sa.Column("head", sa.String(32), nullable=False),
        sa.Column("asof", sa.Date(), nullable=False),
        sa.Column("valid_from", TS, nullable=False),
        sa.Column("valid_to", TS, nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("score_type", sa.String(32), nullable=False),
        sa.Column("risk", sa.Float(), nullable=True),
        sa.Column("priority_score", sa.Float(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("in_budget", sa.Boolean(), nullable=False),
        sa.Column("obj_id", sa.String(32), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("address", sa.JSON(), nullable=False),
        sa.Column("factors", sa.JSON(), nullable=False),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("data_status", sa.String(16), nullable=False),
        sa.Column("case_key", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("first_run_id", BigIntPK, sa.ForeignKey("forecast_runs.id"), nullable=False),
        sa.Column("last_run_id", BigIntPK, sa.ForeignKey("forecast_runs.id"), nullable=False),
    )
    _index("forecasts", "scenario", "asof", "obj_id", "case_key")

    op.create_table(
        "forecast_versions",
        _id(),
        sa.Column("forecast_id", sa.String(64), sa.ForeignKey("forecasts.id"), nullable=False),
        sa.Column("run_id", BigIntPK, sa.ForeignKey("forecast_runs.id"), nullable=False),
        sa.Column("risk", sa.Float(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("recorded_at", TS, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    _index("forecast_versions", "forecast_id")

    op.create_table(
        "issued_log",
        _id(),
        sa.Column("head", sa.String(32), nullable=False),
        sa.Column("asof", sa.Date(), nullable=False),
        sa.Column("entity_key", sa.String(64), nullable=False),
        sa.Column("forecast_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("head", "asof", "entity_key"),
    )
    _index("issued_log", "head", "asof")

    op.create_table(
        "reason_codes",
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("actions", sa.JSON(), nullable=False),
    )

    op.create_table(
        "decisions",
        _id(),
        sa.Column("forecast_id", sa.String(64), sa.ForeignKey("forecasts.id"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(32), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("author", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
    )
    _index("decisions", "forecast_id")

    op.create_table(
        "outcomes",
        sa.Column("forecast_id", sa.String(64), sa.ForeignKey("forecasts.id"),
                  primary_key=True),
        sa.Column("outcome_auto", sa.String(16), nullable=True),
        sa.Column("outcome_manual", sa.String(32), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("event_at", TS, nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("author", sa.String(64), nullable=True),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
    )

    op.create_table(
        "work_orders",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("forecast_ids", sa.JSON(), nullable=False),
        sa.Column("scenario", sa.String(32), nullable=False),
        sa.Column("obj_id", sa.String(32), nullable=True),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("work_type", sa.String(200), nullable=False),
        sa.Column("due_by", TS, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("rationale", sa.JSON(), nullable=False),
        sa.Column("pickets", sa.JSON(), nullable=False),
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
    )
    _index("work_orders", "scenario", "obj_id", "status")

    op.create_table(
        "work_order_history",
        _id(),
        sa.Column("order_id", sa.String(64), sa.ForeignKey("work_orders.id"), nullable=False),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("author", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("at", TS, nullable=False),
    )
    _index("work_order_history", "order_id")

    op.create_table(
        "ingest_batches",
        _id(),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("received_at", TS, nullable=False),
        sa.Column("rows_total", sa.Integer(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("outside_demo_window", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
    )

    op.create_table(
        "events",
        _id(),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("alarm", sa.Boolean(), nullable=False),
        sa.Column("val_raw", sa.Text(), nullable=True),
        sa.Column("val_num", sa.Float(), nullable=True),
        sa.Column("event_class", sa.String(16), nullable=False),
        sa.Column("hint", sa.String(200), nullable=True),
        sa.Column("batch_id", BigIntPK, sa.ForeignKey("ingest_batches.id"), nullable=True),
        sa.Column("row_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("row_hash"),
    )
    _index("events", "channel_id", "ts")

    op.create_table(
        "ods_journal",
        _id(),
        sa.Column("ts", TS, nullable=False),
        sa.Column("obj_id", sa.String(32), nullable=True),
        sa.Column("record_type", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("batch_id", BigIntPK, sa.ForeignKey("ingest_batches.id"), nullable=True),
    )
    _index("ods_journal", "ts")

    op.create_table(
        "notifications",
        _id(),
        sa.Column("ts", TS, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("read_by", sa.JSON(), nullable=False),
    )
    _index("notifications", "ts")

    op.create_table(
        "audit_log",
        _id(),
        sa.Column("ts", TS, nullable=False),
        sa.Column("user_login", sa.String(64), nullable=True),
        sa.Column("role", sa.String(32), nullable=True),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("entity", sa.String(200), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
    )
    _index("audit_log", "ts", "user_login")

    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
    )


# Обратный порядок создания: сначала таблицы со ссылками, потом те, на кого ссылаются.
TABLES = ["settings", "audit_log", "notifications", "ods_journal", "events", "ingest_batches",
          "work_order_history", "work_orders", "outcomes", "decisions", "reason_codes",
          "issued_log", "forecast_versions", "forecasts", "forecast_runs", "ref_channels",
          "ref_objects", "users"]


def downgrade() -> None:
    for table in TABLES:
        op.drop_table(table)
