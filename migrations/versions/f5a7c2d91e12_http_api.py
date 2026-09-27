"""HTTP capture identity and transient Ask transport

Revision ID: f5a7c2d91e12
Revises: 9c2e7a4d1b63
Create Date: 2026-09-27
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f5a7c2d91e12"
down_revision: str | None = "9c2e7a4d1b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable HTTP identities while preserving every existing Item and AskJob."""
    op.add_column("items", sa.Column("external_idempotency_key", sa.String(length=128)))
    op.add_column("items", sa.Column("external_request_hash", sa.String(length=64)))
    op.create_index(
        "uq_items_user_external_idempotency",
        "items",
        ["user_id", "external_idempotency_key"],
        unique=True,
        sqlite_where=sa.text("external_idempotency_key IS NOT NULL"),
    )

    op.add_column(
        "ask_jobs",
        sa.Column(
            "response_channel",
            sa.String(length=16),
            sa.CheckConstraint(
                "response_channel IN ('TELEGRAM', 'HTTP')",
                name="ck_ask_jobs_response_channel",
            ),
            server_default=sa.text("'TELEGRAM'"),
            nullable=False,
        ),
    )
    op.add_column("ask_jobs", sa.Column("external_idempotency_key", sa.String(length=128)))
    op.add_column("ask_jobs", sa.Column("external_request_hash", sa.String(length=64)))
    op.add_column("ask_jobs", sa.Column("result_json", sa.JSON(), nullable=True))
    op.add_column("ask_jobs", sa.Column("result_expires_at", sa.DateTime(), nullable=True))
    op.create_index(
        "uq_ask_jobs_user_external_idempotency",
        "ask_jobs",
        ["user_id", "external_idempotency_key"],
        unique=True,
        sqlite_where=sa.text("external_idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_ask_jobs_http_result_expiry",
        "ask_jobs",
        ["result_expires_at"],
        unique=False,
        sqlite_where=sa.text("response_channel = 'HTTP' AND result_json IS NOT NULL"),
    )


def downgrade() -> None:
    """Refuse to discard accepted HTTP identities or transient Ask answers."""
    bind = op.get_bind()
    item_count = bind.scalar(
        sa.text("SELECT COUNT(*) FROM items WHERE external_idempotency_key IS NOT NULL")
    )
    ask_count = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM ask_jobs WHERE response_channel = 'HTTP' "
            "OR external_idempotency_key IS NOT NULL OR result_json IS NOT NULL"
        )
    )
    if item_count or ask_count:
        raise RuntimeError("Cannot downgrade while HTTP capture or Ask data exists.")

    op.drop_index("ix_ask_jobs_http_result_expiry", table_name="ask_jobs")
    op.drop_index("uq_ask_jobs_user_external_idempotency", table_name="ask_jobs")
    with op.batch_alter_table("ask_jobs") as batch:
        batch.drop_constraint("ck_ask_jobs_response_channel", type_="check")
        batch.drop_column("result_expires_at")
        batch.drop_column("result_json")
        batch.drop_column("external_request_hash")
        batch.drop_column("external_idempotency_key")
        batch.drop_column("response_channel")

    op.drop_index("uq_items_user_external_idempotency", table_name="items")
    with op.batch_alter_table("items") as batch:
        batch.drop_column("external_request_hash")
        batch.drop_column("external_idempotency_key")
