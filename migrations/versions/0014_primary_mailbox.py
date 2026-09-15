"""Add an explicit primary mailbox."""

from alembic import op
import sqlalchemy as sa


revision = "0014_primary_mailbox"
down_revision = "0013_email_event_sheet_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mail_accounts", sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_mail_accounts_is_primary", "mail_accounts", ["is_primary"])
    op.execute("""
        UPDATE mail_accounts
        SET is_primary = TRUE
        WHERE id = (
            SELECT id FROM mail_accounts WHERE active IS TRUE ORDER BY created_at LIMIT 1
        )
    """)


def downgrade() -> None:
    op.drop_index("ix_mail_accounts_is_primary", table_name="mail_accounts")
    op.drop_column("mail_accounts", "is_primary")
