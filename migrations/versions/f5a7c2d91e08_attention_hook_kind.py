"""allow grounded attention hook projections in contents

Revision ID: f5a7c2d91e08
Revises: f5a7c2d91e07
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5a7c2d91e08"
down_revision: str | None = "f5a7c2d91e07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIOUS_CONTENT_KINDS = (
    "'USER_TEXT', 'WEB_TEXT', 'TRANSCRIPT', 'VISUAL_NOTES', 'DESCRIPTION', "
    "'CHUNK_SUMMARY', 'TRANSCRIPT_CHUNK', 'DOCUMENT_TEXT'"
)
_CONTENT_KINDS = f"{_PREVIOUS_CONTENT_KINDS}, 'ATTENTION_HOOK'"


def upgrade() -> None:
    """Extend only the existing Content kind constraint, retaining every row."""
    with op.batch_alter_table("contents") as batch:
        batch.drop_constraint("ck_contents_kind", type_="check")
        batch.create_check_constraint("ck_contents_kind", f"kind IN ({_CONTENT_KINDS})")


def downgrade() -> None:
    """Refuse rollback while derived hook rows still require the new enum value."""
    hook_count = op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM contents WHERE kind = 'ATTENTION_HOOK'")
    )
    if hook_count:
        raise RuntimeError(
            "Cannot downgrade while ATTENTION_HOOK content exists; remove derived hooks first."
        )
    with op.batch_alter_table("contents") as batch:
        batch.drop_constraint("ck_contents_kind", type_="check")
        batch.create_check_constraint("ck_contents_kind", f"kind IN ({_PREVIOUS_CONTENT_KINDS})")
