"""add source provenance metadata

Revision ID: 7a6f2d1c9b84
Revises: f4c1a9d2e7b0
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a6f2d1c9b84"
down_revision: str | None = "f4c1a9d2e7b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # JSON keeps source-envelope provenance extensible for PM-03/PM-04 without
    # introducing forwarding-only nullable columns into canonical Item state.
    with op.batch_alter_table("items") as batch_op:
        batch_op.add_column(sa.Column("source_metadata_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("items") as batch_op:
        batch_op.drop_column("source_metadata_json")
