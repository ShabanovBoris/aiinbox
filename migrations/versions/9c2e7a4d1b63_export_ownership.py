"""durable user export jobs and delivery identity

Revision ID: 9c2e7a4d1b63
Revises: f5a7c2d91e11
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "9c2e7a4d1b63"
down_revision: str | None = "f5a7c2d91e11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PREVIOUS_DELIVERY_SOURCE_CHECK = (
    "(item_id IS NOT NULL AND profile_update_job_id IS NULL AND ask_job_id IS NULL) OR "
    "(item_id IS NULL AND profile_update_job_id IS NOT NULL AND ask_job_id IS NULL) OR "
    "(item_id IS NULL AND profile_update_job_id IS NULL AND ask_job_id IS NOT NULL)"
)
_DELIVERY_SOURCE_CHECK = (
    "(item_id IS NOT NULL AND profile_update_job_id IS NULL AND ask_job_id IS NULL "
    "AND export_job_id IS NULL) OR "
    "(item_id IS NULL AND profile_update_job_id IS NOT NULL AND ask_job_id IS NULL "
    "AND export_job_id IS NULL) OR "
    "(item_id IS NULL AND profile_update_job_id IS NULL AND ask_job_id IS NOT NULL "
    "AND export_job_id IS NULL) OR "
    "(item_id IS NULL AND profile_update_job_id IS NULL AND ask_job_id IS NULL "
    "AND export_job_id IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "export_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'PENDING'"), nullable=False
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("mode IN ('COMPACT', 'FULL')", name="ck_export_jobs_mode"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'DONE', 'FAILED')",
            name="ck_export_jobs_status",
        ),
        sa.UniqueConstraint(
            "user_id", "telegram_message_id", name="uq_export_jobs_user_telegram_message"
        ),
    )
    op.create_index("ix_export_jobs_user_id", "export_jobs", ["user_id"])
    op.create_index("ix_export_jobs_status", "export_jobs", ["status"])

    # SQLite's table copy extends the four-way outbox identity while preserving every old row.
    with op.batch_alter_table("deliveries", recreate="always") as batch:
        batch.drop_constraint("ck_deliveries_single_source", type_="check")
        batch.add_column(sa.Column("export_job_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_deliveries_export_job_id_export_jobs", "export_jobs", ["export_job_id"], ["id"]
        )
        batch.create_check_constraint("ck_deliveries_single_source", _DELIVERY_SOURCE_CHECK)
        batch.create_unique_constraint("uq_deliveries_export_job_type", ["export_job_id", "type"])
        batch.create_index("ix_deliveries_export_job_id", ["export_job_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    job_count = bind.scalar(sa.text("SELECT COUNT(*) FROM export_jobs"))
    delivery_count = bind.scalar(
        sa.text("SELECT COUNT(*) FROM deliveries WHERE export_job_id IS NOT NULL")
    )
    if job_count or delivery_count:
        raise RuntimeError("Cannot downgrade while ExportJob rows or export deliveries exist.")

    with op.batch_alter_table("deliveries", recreate="always") as batch:
        batch.drop_index("ix_deliveries_export_job_id")
        batch.drop_constraint("uq_deliveries_export_job_type", type_="unique")
        batch.drop_constraint("ck_deliveries_single_source", type_="check")
        batch.drop_constraint("fk_deliveries_export_job_id_export_jobs", type_="foreignkey")
        batch.drop_column("export_job_id")
        batch.create_check_constraint(
            "ck_deliveries_single_source", _PREVIOUS_DELIVERY_SOURCE_CHECK
        )
    op.drop_index("ix_export_jobs_status", table_name="export_jobs")
    op.drop_index("ix_export_jobs_user_id", table_name="export_jobs")
    op.drop_table("export_jobs")
