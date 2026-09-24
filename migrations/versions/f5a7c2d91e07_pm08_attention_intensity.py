"""add PM-08 rollout settings and fence reminder delivery claims

Revision ID: f5a7c2d91e07
Revises: f5a7c2d91e06
Create Date: 2026-09-24
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f5a7c2d91e07"
down_revision: str | None = "f5a7c2d91e06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing users explicitly start disabled; JSON patching only fills absent
    # keys so saved notification preferences and any prior PM-08 choice survive.
    op.execute(
        """
        UPDATE users
        SET settings_json = json_patch(
            json_patch(
                COALESCE(settings_json, json('{}')),
                CASE
                    WHEN json_type(settings_json, '$.attention_enabled') IS NULL
                    THEN json_object('attention_enabled', json('false'))
                    ELSE json('{}')
                END
            ),
            CASE
                WHEN json_type(settings_json, '$.attention_intensity') IS NULL
                THEN json_object('attention_intensity', 3)
                ELSE json('{}')
            END
        )
        """
    )
    op.add_column("reminders", sa.Column("claimed_at", sa.DateTime(), nullable=True))
    op.add_column(
        "reminders",
        sa.Column("claim_generation", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index(
        "uq_reminders_open_proactive_user",
        "reminders",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("type = 'PROACTIVE_ATTENTION' AND status IN ('PENDING', 'CLAIMED')"),
    )


def downgrade() -> None:
    op.drop_index("uq_reminders_open_proactive_user", table_name="reminders")
    with op.batch_alter_table("reminders") as batch_op:
        batch_op.drop_column("claim_generation")
        batch_op.drop_column("claimed_at")
    # Leave the two JSON keys in place: removing them could erase settings the
    # user changed after upgrade, while older application versions ignore them.
