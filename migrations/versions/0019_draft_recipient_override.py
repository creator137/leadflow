"""Track manual recipient overrides on proposal drafts.

Revision ID: 0019_draft_recipient_override
Revises: 0018_daily_direction_search
"""
from alembic import op
import sqlalchemy as sa


revision = "0019_draft_recipient_override"
down_revision = "0018_daily_direction_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sheet_personalization_drafts",
        sa.Column("recipient_manually_overridden", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("sheet_personalization_drafts", "recipient_manually_overridden", server_default=None)


def downgrade() -> None:
    op.drop_column("sheet_personalization_drafts", "recipient_manually_overridden")
