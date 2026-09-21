"""Track IMAP checkpoints per folder and retain the reply source folder.

Revision ID: 0020_imap_junk_checkpoints
Revises: 0019_draft_recipient_override
"""
from alembic import op
import sqlalchemy as sa


revision = "0020_imap_junk_checkpoints"
down_revision = "0019_draft_recipient_override"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("inbound_replies", sa.Column("imap_folder", sa.String(length=255), nullable=False, server_default="INBOX"))
    op.drop_constraint("uq_inbound_mailbox_uid", "inbound_replies", type_="unique")
    op.create_unique_constraint("uq_inbound_mailbox_uid", "inbound_replies", ["mailbox_id", "imap_folder", "imap_uidvalidity", "imap_uid"])
    op.create_table(
        "imap_folder_checkpoints",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("mailbox_id", sa.String(length=36), nullable=False),
        sa.Column("folder", sa.String(length=255), nullable=False),
        sa.Column("uidvalidity", sa.Integer(), nullable=True),
        sa.Column("last_uid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["mailbox_id"], ["mail_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mailbox_id", "folder", name="uq_imap_checkpoint_folder"),
    )
    op.create_index("ix_imap_folder_checkpoints_mailbox_id", "imap_folder_checkpoints", ["mailbox_id"])
    op.alter_column("inbound_replies", "imap_folder", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_imap_folder_checkpoints_mailbox_id", table_name="imap_folder_checkpoints")
    op.drop_table("imap_folder_checkpoints")
    op.drop_constraint("uq_inbound_mailbox_uid", "inbound_replies", type_="unique")
    op.create_unique_constraint("uq_inbound_mailbox_uid", "inbound_replies", ["mailbox_id", "imap_uidvalidity", "imap_uid"])
    op.drop_column("inbound_replies", "imap_folder")
