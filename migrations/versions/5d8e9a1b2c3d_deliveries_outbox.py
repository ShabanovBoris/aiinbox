"""durable immediate delivery outbox

Revision ID: 5d8e9a1b2c3d
Revises: a1b2c3d4e5f6
Create Date: 2026-09-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "5d8e9a1b2c3d"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id"), nullable=True),
        sa.Column(
            "profile_update_job_id",
            sa.Integer(),
            sa.ForeignKey("profile_update_jobs.id"),
            nullable=True,
        ),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'PENDING'"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "(item_id IS NOT NULL AND profile_update_job_id IS NULL) OR "
            "(item_id IS NULL AND profile_update_job_id IS NOT NULL)",
            name="ck_deliveries_single_source",
        ),
        sa.UniqueConstraint("item_id", "type", name="uq_deliveries_item_type"),
        sa.UniqueConstraint("profile_update_job_id", "type", name="uq_deliveries_profile_job_type"),
    )
    op.create_index("ix_deliveries_user_id", "deliveries", ["user_id"])
    op.create_index("ix_deliveries_item_id", "deliveries", ["item_id"])
    op.create_index("ix_deliveries_profile_update_job_id", "deliveries", ["profile_update_job_id"])
    op.create_index("ix_deliveries_type", "deliveries", ["type"])
    op.create_index("ix_deliveries_status", "deliveries", ["status"])


def downgrade() -> None:
    op.drop_index("ix_deliveries_status", table_name="deliveries")
    op.drop_index("ix_deliveries_type", table_name="deliveries")
    op.drop_index("ix_deliveries_profile_update_job_id", table_name="deliveries")
    op.drop_index("ix_deliveries_item_id", table_name="deliveries")
    op.drop_index("ix_deliveries_user_id", table_name="deliveries")
    op.drop_table("deliveries")
