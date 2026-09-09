"""Require one direction per Google Sheet tab."""

from alembic import op

revision = "0004_unique_direction_sheet_tab"
down_revision = "0003_directions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_directions_sheet_tab", "directions", ["sheet_tab"])


def downgrade() -> None:
    op.drop_constraint("uq_directions_sheet_tab", "directions", type_="unique")
