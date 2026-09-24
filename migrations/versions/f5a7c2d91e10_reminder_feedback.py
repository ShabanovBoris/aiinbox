"""attribute reminder reactions without losing existing Event history

Revision ID: f5a7c2d91e10
Revises: f5a7c2d91e09
Create Date: 2026-09-24
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f5a7c2d91e10"
down_revision: str | None = "f5a7c2d91e09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REMINDER_EVENT_TYPES = (
    "'REMINDER_SENT', 'REMINDER_OPENED', 'REMINDER_SNOOZED', "
    "'REMINDER_DONE', 'REMINDER_DISMISSED', 'REMINDER_DISLIKED'"
)


def upgrade() -> None:
    """Rebuild only Event references; old Events are copied without synthetic history."""
    # Batch recreation preserves every Event column/value while SQLite changes
    # nullability and adds the cross-table reference/check constraints.
    op.drop_index("uq_events_user_idempotency_key", table_name="events")
    with op.batch_alter_table("events", recreate="always") as batch:
        batch.alter_column(
            "item_id",
            existing_type=sa.Integer(),
            existing_nullable=False,
            nullable=True,
        )
        batch.add_column(sa.Column("reminder_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_events_reminder_id_reminders", "reminders", ["reminder_id"], ["id"]
        )
        batch.create_check_constraint(
            "ck_events_item_or_reminder_ref",
            "item_id IS NOT NULL OR reminder_id IS NOT NULL",
        )

    # The prior idempotency contract is explicitly recreated after the table copy.
    op.create_index(
        "uq_events_user_idempotency_key",
        "events",
        ["user_id", "idempotency_key"],
        unique=True,
    )
    op.create_index("ix_events_reminder_id", "events", ["reminder_id"])
    op.create_index(
        "uq_events_reminder_event_type",
        "events",
        ["reminder_id", "event_type"],
        unique=True,
        sqlite_where=sa.text(
            f"reminder_id IS NOT NULL AND event_type IN ({_REMINDER_EVENT_TYPES})"
        ),
    )


def downgrade() -> None:
    """Refuse rollback rather than discard any Reminder attribution."""
    bind = op.get_bind()
    reminder_event_count = bind.scalar(
        sa.text("SELECT COUNT(*) FROM events WHERE reminder_id IS NOT NULL")
    )
    if reminder_event_count:
        raise RuntimeError("Cannot downgrade while Reminder-attributed Events exist.")

    op.drop_index("uq_events_reminder_event_type", table_name="events")
    op.drop_index("ix_events_reminder_id", table_name="events")
    op.drop_index("uq_events_user_idempotency_key", table_name="events")
    with op.batch_alter_table("events", recreate="always") as batch:
        batch.drop_constraint("ck_events_item_or_reminder_ref", type_="check")
        batch.drop_constraint("fk_events_reminder_id_reminders", type_="foreignkey")
        batch.drop_column("reminder_id")
        batch.alter_column(
            "item_id",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
        )
    op.create_index(
        "uq_events_user_idempotency_key",
        "events",
        ["user_id", "idempotency_key"],
        unique=True,
    )
