"""Уникальный ключ новых записей ОДС для конкурентного повтора.

Старые записи остаются без ключа: существующие дубли не удаляем и исторические
счётчики загрузок не меняем. Повтор относительно них по-прежнему находит SELECT.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_ods_ingest_key"
down_revision: str | Sequence[str] | None = "0003_revoked_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ods_journal") as batch:
        batch.add_column(sa.Column("ingest_key", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_ods_journal_ingest_key", ["ingest_key"])


def downgrade() -> None:
    with op.batch_alter_table("ods_journal") as batch:
        batch.drop_constraint("uq_ods_journal_ingest_key", type_="unique")
        batch.drop_column("ingest_key")
