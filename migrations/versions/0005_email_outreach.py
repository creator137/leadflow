"""Production email outreach queue, mailbox checkpoints and analytics fields."""

from alembic import op
import sqlalchemy as sa

revision = "0005_email_outreach"
down_revision = "0004_unique_direction_sheet_tab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mail_accounts", sa.Column("reply_to", sa.String(320)))
    op.add_column("mail_accounts", sa.Column("imap_security", sa.String(16), nullable=False, server_default="ssl"))
    op.add_column("mail_accounts", sa.Column("imap_uidvalidity", sa.Integer()))
    op.add_column("mail_accounts", sa.Column("imap_last_uid", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("mail_accounts", sa.Column("forward_replies_to", sa.String(320)))
    op.add_column("mail_accounts", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))

    op.add_column("email_templates", sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="SET NULL")))
    op.add_column("email_templates", sa.Column("text_template", sa.Text(), nullable=False, server_default=""))
    op.add_column("email_templates", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_email_templates_direction_id", "email_templates", ["direction_id"])

    op.add_column("campaigns", sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="SET NULL")))
    op.add_column("campaigns", sa.Column("sending_interval_seconds", sa.Integer(), nullable=False, server_default="60"))
    op.add_column("campaigns", sa.Column("cooldown_days", sa.Integer(), nullable=False, server_default="30"))
    op.add_column("campaigns", sa.Column("status", sa.String(32), nullable=False, server_default="paused"))
    op.add_column("campaigns", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_campaigns_direction_id", "campaigns", ["direction_id"])
    op.create_index("ix_campaigns_status", "campaigns", ["status"])

    op.drop_index("uq_delivery_company_recipient", table_name="email_deliveries")
    for name, column, type_ in [
        ("direction_id", "direction_id", sa.String(36)),
        ("recipient_name", "recipient_name", sa.String(255)),
        ("text_body", "text_body", sa.Text()),
        ("send_mode", "send_mode", sa.String(32)),
        ("unsubscribe_token", "unsubscribe_token", sa.String(64)),
        ("provider_message_id", "provider_message_id", sa.String(998)),
        ("attempt_count", "attempt_count", sa.Integer()),
        ("max_attempts", "max_attempts", sa.Integer()),
        ("next_attempt_at", "next_attempt_at", sa.DateTime(timezone=True)),
        ("locked_at", "locked_at", sa.DateTime(timezone=True)),
        ("locked_by", "locked_by", sa.String(100)),
        ("created_at", "created_at", sa.DateTime(timezone=True)),
        ("updated_at", "updated_at", sa.DateTime(timezone=True)),
    ]:
        op.add_column("email_deliveries", sa.Column(column, type_))
    op.create_foreign_key("fk_delivery_direction", "email_deliveries", "directions", ["direction_id"], ["id"], ondelete="SET NULL")
    op.execute("UPDATE email_deliveries SET text_body='', send_mode='campaign', unsubscribe_token=tracking_token, attempt_count=CASE WHEN sent_at IS NULL THEN 0 ELSE 1 END, max_attempts=3, created_at=COALESCE(sent_at, now()), updated_at=now()")
    for column in ["text_body", "send_mode", "unsubscribe_token", "attempt_count", "max_attempts", "created_at", "updated_at"]:
        op.alter_column("email_deliveries", column, nullable=False)
    op.create_index("ix_email_deliveries_direction_id", "email_deliveries", ["direction_id"])
    op.create_index("ix_email_deliveries_unsubscribe_token", "email_deliveries", ["unsubscribe_token"], unique=True)
    op.create_index("ix_email_deliveries_next_attempt_at", "email_deliveries", ["next_attempt_at"])
    op.create_index("ix_email_deliveries_locked_at", "email_deliveries", ["locked_at"])
    op.create_index("uq_delivery_campaign_company_recipient", "email_deliveries", ["campaign_id", "company_id", "recipient_email"], unique=True)

    op.add_column("suppressions", sa.Column("source_delivery_id", sa.String(36), sa.ForeignKey("email_deliveries.id", ondelete="SET NULL")))
    op.add_column("suppressions", sa.Column("note", sa.Text()))
    op.add_column("suppressions", sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()))

    op.add_column("inbound_replies", sa.Column("imap_uid", sa.Integer()))
    op.add_column("inbound_replies", sa.Column("imap_uidvalidity", sa.Integer()))
    op.add_column("inbound_replies", sa.Column("in_reply_to", sa.String(998)))
    op.add_column("inbound_replies", sa.Column("references", sa.Text()))
    op.add_column("inbound_replies", sa.Column("bounce_type", sa.String(32)))
    op.add_column("inbound_replies", sa.Column("forwarded_at", sa.DateTime(timezone=True)))
    op.create_unique_constraint("uq_inbound_mailbox_uid", "inbound_replies", ["mailbox_id", "imap_uidvalidity", "imap_uid"])


def downgrade() -> None:
    op.drop_constraint("uq_inbound_mailbox_uid", "inbound_replies", type_="unique")
    for column in ["forwarded_at", "bounce_type", "references", "in_reply_to", "imap_uidvalidity", "imap_uid"]:
        op.drop_column("inbound_replies", column)
    for column in ["active", "note", "source_delivery_id"]:
        op.drop_column("suppressions", column)
    op.drop_index("uq_delivery_campaign_company_recipient", table_name="email_deliveries")
    for index in ["ix_email_deliveries_locked_at", "ix_email_deliveries_next_attempt_at", "ix_email_deliveries_unsubscribe_token", "ix_email_deliveries_direction_id"]:
        op.drop_index(index, table_name="email_deliveries")
    op.drop_constraint("fk_delivery_direction", "email_deliveries", type_="foreignkey")
    for column in ["updated_at", "created_at", "locked_by", "locked_at", "next_attempt_at", "max_attempts", "attempt_count", "provider_message_id", "unsubscribe_token", "send_mode", "text_body", "recipient_name", "direction_id"]:
        op.drop_column("email_deliveries", column)
    op.create_index("uq_delivery_company_recipient", "email_deliveries", ["company_id", "recipient_email"], unique=True)
    for index in ["ix_campaigns_status", "ix_campaigns_direction_id"]:
        op.drop_index(index, table_name="campaigns")
    for column in ["updated_at", "status", "cooldown_days", "sending_interval_seconds", "direction_id"]:
        op.drop_column("campaigns", column)
    op.drop_index("ix_email_templates_direction_id", table_name="email_templates")
    for column in ["updated_at", "text_template", "direction_id"]:
        op.drop_column("email_templates", column)
    for column in ["updated_at", "forward_replies_to", "imap_last_uid", "imap_uidvalidity", "imap_security", "reply_to"]:
        op.drop_column("mail_accounts", column)
