"""durable standalone Ask jobs and outbox identity

Revision ID: f5a7c2d91e11
Revises: f5a7c2d91e10
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f5a7c2d91e11"
down_revision: str | None = "f5a7c2d91e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_DELIVERY_SOURCE_CHECK = (
    "(item_id IS NOT NULL AND profile_update_job_id IS NULL) OR "
    "(item_id IS NULL AND profile_update_job_id IS NOT NULL)"
)
_DELIVERY_SOURCE_CHECK = (
    "(item_id IS NOT NULL AND profile_update_job_id IS NULL AND ask_job_id IS NULL) OR "
    "(item_id IS NULL AND profile_update_job_id IS NOT NULL AND ask_job_id IS NULL) OR "
    "(item_id IS NULL AND profile_update_job_id IS NULL AND ask_job_id IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "ask_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'PENDING'"), nullable=False
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "user_id", "telegram_message_id", name="uq_ask_jobs_user_telegram_message"
        ),
    )
    op.create_index("ix_ask_jobs_user_id", "ask_jobs", ["user_id"])
    op.create_index("ix_ask_jobs_status", "ask_jobs", ["status"])

    # SQLite rewrites the outbox table to extend its source XOR constraint; batch
    # copy preserves every old delivery field and row during the schema change.
    with op.batch_alter_table("deliveries", recreate="always") as batch:
        batch.drop_constraint("ck_deliveries_single_source", type_="check")
        batch.add_column(sa.Column("ask_job_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_deliveries_ask_job_id_ask_jobs", "ask_jobs", ["ask_job_id"], ["id"]
        )
        batch.create_check_constraint("ck_deliveries_single_source", _DELIVERY_SOURCE_CHECK)
        batch.create_unique_constraint("uq_deliveries_ask_job_type", ["ask_job_id", "type"])
        batch.create_index("ix_deliveries_ask_job_id", ["ask_job_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    ask_job_count = bind.scalar(sa.text("SELECT COUNT(*) FROM ask_jobs"))
    ask_delivery_count = bind.scalar(
        sa.text("SELECT COUNT(*) FROM deliveries WHERE ask_job_id IS NOT NULL")
    )
    if ask_job_count or ask_delivery_count:
        raise RuntimeError("Cannot downgrade while Ask jobs or Ask deliveries exist.")

    with op.batch_alter_table("deliveries", recreate="always") as batch:
        batch.drop_index("ix_deliveries_ask_job_id")
        batch.drop_constraint("uq_deliveries_ask_job_type", type_="unique")
        batch.drop_constraint("ck_deliveries_single_source", type_="check")
        batch.drop_constraint("fk_deliveries_ask_job_id_ask_jobs", type_="foreignkey")
        batch.drop_column("ask_job_id")
        batch.create_check_constraint("ck_deliveries_single_source", _OLD_DELIVERY_SOURCE_CHECK)
    op.drop_index("ix_ask_jobs_status", table_name="ask_jobs")
    op.drop_index("ix_ask_jobs_user_id", table_name="ask_jobs")
    op.drop_table("ask_jobs")
