"""constrain source enums including document values

Revision ID: d9f3b1a7c5e2
Revises: c2d4e6f8a0b1
Create Date: 2026-09-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d9f3b1a7c5e2"
down_revision: str | None = "c2d4e6f8a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SOURCE_TYPES = "'TEXT', 'WEB', 'VOICE', 'AUDIO', 'YOUTUBE', 'VIDEO', 'DOCUMENT'"
_CONTENT_KINDS = (
    "'USER_TEXT', 'WEB_TEXT', 'TRANSCRIPT', 'VISUAL_NOTES', 'DESCRIPTION', "
    "'CHUNK_SUMMARY', 'TRANSCRIPT_CHUNK', 'DOCUMENT_TEXT'"
)


def upgrade() -> None:
    # SQLAlchemy's non-native enums were VARCHAR columns without SQLite checks;
    # create explicit constraints so fresh and upgraded databases enforce the same values.
    with op.batch_alter_table("items") as batch:
        batch.create_check_constraint("ck_items_source_type", f"source_type IN ({_SOURCE_TYPES})")
    with op.batch_alter_table("item_sources") as batch:
        batch.create_check_constraint(
            "ck_item_sources_source_type", f"source_type IN ({_SOURCE_TYPES})"
        )
    with op.batch_alter_table("contents") as batch:
        batch.create_check_constraint("ck_contents_kind", f"kind IN ({_CONTENT_KINDS})")


def downgrade() -> None:
    # Keep rows intact; removing these checks makes the previous extensible VARCHAR schema.
    with op.batch_alter_table("contents") as batch:
        batch.drop_constraint("ck_contents_kind", type_="check")
    with op.batch_alter_table("item_sources") as batch:
        batch.drop_constraint("ck_item_sources_source_type", type_="check")
    with op.batch_alter_table("items") as batch:
        batch.drop_constraint("ck_items_source_type", type_="check")
