"""Harden outbound email deliverability metadata."""

from alembic import op
import sqlalchemy as sa


revision = "0015_email_deliverability"
down_revision = "0014_primary_mailbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("email_deliveries", sa.Column("unsubscribe_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("email_deliveries", "unsubscribe_url")
