"""Track last exported values to distinguish manual sheet edits."""

from alembic import op
import sqlalchemy as sa

revision = "0008_sheet_sync_snapshots"
down_revision = "0007_analytics_observability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sheet_row_mappings", sa.Column("last_synced_values", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))


def downgrade() -> None:
    op.drop_column("sheet_row_mappings", "last_synced_values")
