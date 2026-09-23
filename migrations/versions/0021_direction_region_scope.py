"""Support an administrative-region search location.

Revision ID: 0021_direction_region_scope
Revises: 0020_imap_junk_checkpoints
"""
from alembic import op
import sqlalchemy as sa


revision = "0021_direction_region_scope"
down_revision = "0020_imap_junk_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "direction_locations",
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="city"),
    )
    op.create_check_constraint(
        "ck_direction_locations_scope", "direction_locations", "scope IN ('city', 'region')",
    )
    op.alter_column("direction_locations", "scope", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_direction_locations_scope", "direction_locations", type_="check")
    op.drop_column("direction_locations", "scope")
