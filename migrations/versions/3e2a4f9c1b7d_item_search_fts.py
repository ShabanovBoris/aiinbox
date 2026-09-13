"""item search FTS5 index

Revision ID: 3e2a4f9c1b7d
Revises: 76ed20f32fe7
Create Date: 2026-09-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3e2a4f9c1b7d"
down_revision: str | None = "76ed20f32fe7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE VIRTUAL TABLE item_search USING fts5("
        "item_id UNINDEXED, user_id UNINDEXED, title, summary, user_note, tags, content)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE item_search")
