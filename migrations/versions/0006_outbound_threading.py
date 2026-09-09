"""Store outbound reply threading metadata."""

from alembic import op
import sqlalchemy as sa

revision = "0006_outbound_threading"
down_revision = "0005_email_outreach"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("email_deliveries", sa.Column("in_reply_to", sa.String(998)))


def downgrade() -> None:
    op.drop_column("email_deliveries", "in_reply_to")
