"""add composite item sources

Revision ID: c2d4e6f8a0b1
Revises: 7a6f2d1c9b84
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2d4e6f8a0b1"
down_revision: str | None = "7a6f2d1c9b84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ItemSource is the canonical extraction identity for composite Items: one
    # Telegram message may contain several URLs/media while remaining one Item.
    op.create_table(
        "item_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("source_index", sa.Integer(), nullable=False),
        sa.Column(
            "source_type",
            sa.Enum(
                "TEXT",
                "WEB",
                "VOICE",
                "AUDIO",
                "YOUTUBE",
                "VIDEO",
                name="sourcetype",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("source_url", sa.String(length=700), nullable=True),
        sa.Column("source_file_id", sa.String(length=200), nullable=True),
        sa.Column("content_duration_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "extraction_status",
            sa.String(length=16),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "extraction_status IN ('PENDING', 'READY', 'FAILED')",
            name="ck_item_sources_extraction_status",
        ),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", "source_index", name="uq_item_sources_index"),
    )
    with op.batch_alter_table("item_sources") as batch_op:
        batch_op.create_index("ix_item_sources_item_id", ["item_id"], unique=False)
        batch_op.create_index(
            "ix_item_sources_extraction_status", ["extraction_status"], unique=False
        )

    with op.batch_alter_table("contents") as batch_op:
        batch_op.add_column(sa.Column("source_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_contents_source_id_item_sources", "item_sources", ["source_id"], ["id"]
        )
        batch_op.create_index("ix_contents_source_id", ["source_id"], unique=False)

    # Legacy single-source Items are converted into one child source. TEXT Items
    # need no child row because the Telegram message text itself is Item-level content.
    op.execute(
        """
        INSERT INTO item_sources (
            item_id, source_index, source_type, source_url, source_file_id,
            content_duration_seconds, extraction_status
        )
        SELECT id, 0, source_type, source_url, source_file_id,
               content_duration_seconds, 'PENDING'
        FROM items
        WHERE source_type != 'TEXT'
           OR source_url IS NOT NULL
           OR source_file_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE contents
        SET source_id = (
            SELECT s.id FROM item_sources AS s
            WHERE s.item_id = contents.item_id AND s.source_index = 0
        )
        WHERE kind IN ('WEB_TEXT', 'TRANSCRIPT', 'VISUAL_NOTES', 'DESCRIPTION', 'TRANSCRIPT_CHUNK')
          AND EXISTS (
            SELECT 1 FROM item_sources AS s
            WHERE s.item_id = contents.item_id AND s.source_index = 0
          )
        """
    )
    op.execute(
        """
        UPDATE item_sources
        SET extraction_status = 'READY'
        WHERE EXISTS (
            SELECT 1 FROM contents AS c
            WHERE c.source_id = item_sources.id
              AND c.kind IN ('WEB_TEXT', 'TRANSCRIPT')
        )
        """
    )

    # URL identity now belongs to a source inside a message. Two different
    # Telegram messages may legitimately reference the same URL with different context.
    with op.batch_alter_table("items") as batch_op:
        batch_op.drop_constraint("uq_items_user_url", type_="unique")


def downgrade() -> None:
    with op.batch_alter_table("items") as batch_op:
        batch_op.create_unique_constraint("uq_items_user_url", ["user_id", "source_url"])

    with op.batch_alter_table("contents") as batch_op:
        batch_op.drop_index("ix_contents_source_id")
        batch_op.drop_constraint("fk_contents_source_id_item_sources", type_="foreignkey")
        batch_op.drop_column("source_id")

    with op.batch_alter_table("item_sources") as batch_op:
        batch_op.drop_index("ix_item_sources_extraction_status")
        batch_op.drop_index("ix_item_sources_item_id")
    op.drop_table("item_sources")
