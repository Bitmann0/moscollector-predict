"""revoked_sessions: выход отзывает одну сессию на сервере (BE-09).

В токене cookie лежит случайный jti. Выход записывает его сюда вместе со сроком
токена, authenticate() отклоняет записанный jti. Отзывается одна сессия, а не все
сессии логина: под демо-логинами на стенде входят несколько экспертов сразу.
Новая таблица, существующие не меняются, поэтому миграция идёт и на заполненной
БД стенда.

Revision ID: 0003_revoked_sessions
Revises: 0002_event_incident_group
Create Date: 2026-09-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_revoked_sessions"
down_revision: str | Sequence[str] | None = "0002_event_incident_group"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "revoked_sessions",
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_revoked_sessions_expires_at", "revoked_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_table("revoked_sessions")
