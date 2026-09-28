"""events.incident_group: группа аварии по ответу 2 заказчика (analysis/qa_customer_2026-09-28.md).

Колонку и индекс могла уже создать scripts/reclassify_events.py: на стенде она
пересчитывает историю, не поднимая alembic_version, чтобы образ без этой миграции
по-прежнему стартовал. Поэтому миграция создаёт только то, чего в БД ещё нет.
Индекс частичный — по строкам с группой; batch_alter_table нужен SQLite для отката.

Revision ID: 0002_event_incident_group
Revises: 0001_initial
Create Date: 2026-09-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_event_incident_group"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_events_incident_group"
HAS_GROUP = sa.text("incident_group IS NOT NULL")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "incident_group" not in {c["name"] for c in inspector.get_columns("events")}:
        with op.batch_alter_table("events") as batch:
            batch.add_column(sa.Column("incident_group", sa.String(32), nullable=True))
    if INDEX not in {i["name"] for i in inspector.get_indexes("events")}:
        op.create_index(INDEX, "events", ["incident_group"],
                        postgresql_where=HAS_GROUP, sqlite_where=HAS_GROUP)


def downgrade() -> None:
    op.drop_index(INDEX, table_name="events")
    with op.batch_alter_table("events") as batch:
        batch.drop_column("incident_group")
