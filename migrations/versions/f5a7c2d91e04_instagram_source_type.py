"""allow Instagram ItemSources

Revision ID: f5a7c2d91e04
Revises: d9f3b1a7c5e2
Create Date: 2026-09-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f5a7c2d91e04"
down_revision: str | None = "d9f3b1a7c5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SOURCE_TYPES = "'TEXT', 'WEB', 'VOICE', 'AUDIO', 'YOUTUBE', 'INSTAGRAM', 'VIDEO', 'DOCUMENT'"
_PREVIOUS_SOURCE_TYPES = "'TEXT', 'WEB', 'VOICE', 'AUDIO', 'YOUTUBE', 'VIDEO', 'DOCUMENT'"


def upgrade() -> None:
    """Extend both SQLite enum-like checks while copying their existing rows."""
    with op.batch_alter_table("items") as batch:
        batch.drop_constraint("ck_items_source_type", type_="check")
        batch.create_check_constraint("ck_items_source_type", f"source_type IN ({_SOURCE_TYPES})")
    with op.batch_alter_table("item_sources") as batch:
        batch.drop_constraint("ck_item_sources_source_type", type_="check")
        batch.create_check_constraint(
            "ck_item_sources_source_type", f"source_type IN ({_SOURCE_TYPES})"
        )


def downgrade() -> None:
    """Restore the previous allowed values; no existing source rows are rewritten."""
    with op.batch_alter_table("item_sources") as batch:
        batch.drop_constraint("ck_item_sources_source_type", type_="check")
        batch.create_check_constraint(
            "ck_item_sources_source_type", f"source_type IN ({_PREVIOUS_SOURCE_TYPES})"
        )
    with op.batch_alter_table("items") as batch:
        batch.drop_constraint("ck_items_source_type", type_="check")
        batch.create_check_constraint(
            "ck_items_source_type", f"source_type IN ({_PREVIOUS_SOURCE_TYPES})"
        )
