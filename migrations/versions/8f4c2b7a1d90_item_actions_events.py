"""item lifecycle timestamps and feedback events

Revision ID: 8f4c2b7a1d90
Revises: 3e2a4f9c1b7d
Create Date: 2026-09-13
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "8f4c2b7a1d90"
down_revision: str | None = "3e2a4f9c1b7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("items", sa.Column("completed_at", sa.DateTime(), nullable=True))
    op.add_column("items", sa.Column("archived_at", sa.DateTime(), nullable=True))
    op.add_column("items", sa.Column("snoozed_until", sa.DateTime(), nullable=True))
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id"), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_events_user_id", "events", ["user_id"])
    op.create_index("ix_events_item_id", "events", ["item_id"])
    op.create_index("ix_events_event_type", "events", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_events_event_type", table_name="events")
    op.drop_index("ix_events_item_id", table_name="events")
    op.drop_index("ix_events_user_id", table_name="events")
    op.drop_table("events")
    op.drop_column("items", "snoozed_until")
    op.drop_column("items", "archived_at")
    op.drop_column("items", "completed_at")
