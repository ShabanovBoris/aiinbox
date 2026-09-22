"""add explicit user interest level

Revision ID: f4c1a9d2e7b0
Revises: 5d8e9a1b2c3d
Create Date: 2026-09-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f4c1a9d2e7b0"
down_revision: str | None = "5d8e9a1b2c3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite batch mode rebuilds items so the CHECK becomes a real DB boundary;
    # server_default=2 backfills existing rows during the same migration.
    with op.batch_alter_table("items") as batch_op:
        batch_op.add_column(
            sa.Column("interest_level", sa.Integer(), server_default="2", nullable=False)
        )
        batch_op.create_check_constraint(
            "ck_items_interest_level",
            "interest_level BETWEEN 1 AND 3",
        )


def downgrade() -> None:
    with op.batch_alter_table("items") as batch_op:
        batch_op.drop_constraint("ck_items_interest_level", type_="check")
        batch_op.drop_column("interest_level")
