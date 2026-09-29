"""История ручных итогов проверки без изменения текущей таблицы outcomes.

Текущий старый итог переносится как начальная версия. Более ранние исправления
восстановить из старого журнала аудита нельзя; новые версии сохраняются все.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_manual_outcome_revisions"
down_revision: str | Sequence[str] | None = "0004_ods_ingest_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BigIntPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "manual_outcome_revisions",
        sa.Column("id", BigIntPK, primary_key=True, autoincrement=True),
        sa.Column("forecast_id", sa.String(64), sa.ForeignKey("forecasts.id"), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("author", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
    )
    op.create_index("ix_manual_outcome_revisions_forecast_id", "manual_outcome_revisions",
                    ["forecast_id"])
    op.execute(sa.text("""
        INSERT INTO manual_outcome_revisions
            (forecast_id, outcome, comment, event_at, channel_id, author, recorded_at, source)
        SELECT forecast_id, outcome_manual, comment, event_at, channel_id,
               COALESCE(author, 'unknown'), updated_at, source
        FROM outcomes WHERE outcome_manual IS NOT NULL
    """))


def downgrade() -> None:
    op.drop_table("manual_outcome_revisions")
