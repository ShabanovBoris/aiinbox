"""persistent notification settings and reminders

Revision ID: a1b2c3d4e5f6
Revises: 8f4c2b7a1d90
Create Date: 2026-09-14
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "8f4c2b7a1d90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("timezone", sa.String(length=64), server_default=sa.text("'UTC'"), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("settings_json", sa.JSON(), server_default=sa.text("'{}'"), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("daily_digest_enabled_at", sa.DateTime(), nullable=True),
    )
    op.execute("UPDATE users SET timezone = 'UTC' WHERE timezone IS NULL")
    op.execute("UPDATE users SET settings_json = '{}' WHERE settings_json IS NULL")
    op.execute("UPDATE users SET daily_digest_enabled_at = created_at")
    with op.batch_alter_table("users") as batch:
        batch.alter_column("timezone", nullable=False)
        batch.alter_column("settings_json", nullable=False)
    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id"), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "user_id", "item_id", "type", "scheduled_at", name="uq_reminders_delivery"
        ),
    )
    op.create_index("ix_reminders_user_id", "reminders", ["user_id"])
    op.create_index("ix_reminders_item_id", "reminders", ["item_id"])
    op.create_index("ix_reminders_type", "reminders", ["type"])
    op.create_index("ix_reminders_scheduled_at", "reminders", ["scheduled_at"])
    op.create_index("ix_reminders_status", "reminders", ["status"])
    op.create_index(
        "uq_reminders_daily_digest",
        "reminders",
        ["user_id", "type", "scheduled_at"],
        unique=True,
        sqlite_where=sa.text("type = 'DAILY_DIGEST'"),
    )
    op.execute(
        "INSERT INTO reminders (user_id, item_id, type, scheduled_at, status) "
        "SELECT user_id, id, 'SNOOZE_RESURFACE', snoozed_until, 'PENDING' "
        "FROM items WHERE state = 'SNOOZED' AND snoozed_until IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("uq_reminders_daily_digest", table_name="reminders")
    op.drop_index("ix_reminders_status", table_name="reminders")
    op.drop_index("ix_reminders_scheduled_at", table_name="reminders")
    op.drop_index("ix_reminders_type", table_name="reminders")
    op.drop_index("ix_reminders_item_id", table_name="reminders")
    op.drop_index("ix_reminders_user_id", table_name="reminders")
    op.drop_table("reminders")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("settings_json")
        batch.drop_column("timezone")
        batch.drop_column("daily_digest_enabled_at")
