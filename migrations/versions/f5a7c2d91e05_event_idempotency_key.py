"""add durable event idempotency keys

Revision ID: f5a7c2d91e05
Revises: f5a7c2d91e04
Create Date: 2026-09-23
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f5a7c2d91e05"
down_revision: str | None = "f5a7c2d91e04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("idempotency_key", sa.String(length=160), nullable=True))
    op.create_index(
        "uq_events_user_idempotency_key",
        "events",
        ["user_id", "idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_events_user_idempotency_key", table_name="events")
    op.drop_column("events", "idempotency_key")
