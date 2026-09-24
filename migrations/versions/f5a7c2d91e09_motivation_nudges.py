"""add rollout-safe PM-10 settings and durable user-level reminder slots

Revision ID: f5a7c2d91e09
Revises: f5a7c2d91e08
Create Date: 2026-09-24
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f5a7c2d91e09"
down_revision: str | None = "f5a7c2d91e08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing accounts receive explicit OFF unless they already chose a value;
    # application defaults remain ON for users created after this migration.
    op.execute(
        """
        UPDATE users
        SET settings_json = json_patch(
            COALESCE(settings_json, json('{}')),
            CASE
                WHEN json_type(settings_json, '$.generic_motivation_enabled') IS NULL
                THEN json_object('generic_motivation_enabled', json('false'))
                ELSE json('{}')
            END
        )
        """
    )
    # NULL item_id is intentionally handled by a separate identity so SQLite's
    # ordinary multi-column UNIQUE constraint cannot admit duplicate user slots.
    op.create_index(
        "uq_reminders_motivation_slot",
        "reminders",
        ["user_id", "type", "scheduled_at"],
        unique=True,
        sqlite_where=sa.text("type = 'MOTIVATION_NUDGE' AND item_id IS NULL"),
    )
    op.create_index(
        "uq_reminders_open_motivation_user",
        "reminders",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("type = 'MOTIVATION_NUDGE' AND status IN ('PENDING', 'CLAIMED')"),
    )


def downgrade() -> None:
    """Remove PM-10 claim constraints but retain JSON choices for safe rollback."""
    op.drop_index("uq_reminders_open_motivation_user", table_name="reminders")
    op.drop_index("uq_reminders_motivation_slot", table_name="reminders")
