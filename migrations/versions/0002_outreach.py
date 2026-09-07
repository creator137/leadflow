"""Mailing, tracking, integrations and phrase search."""

from alembic import op
import sqlalchemy as sa

revision = "0002_outreach"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("from_email", sa.String(320), nullable=False, unique=True),
        sa.Column("from_name", sa.String(255)),
        sa.Column("smtp_host", sa.String(255), nullable=False),
        sa.Column("smtp_port", sa.Integer(), nullable=False),
        sa.Column("smtp_login", sa.String(320), nullable=False),
        sa.Column("smtp_password_encrypted", sa.Text(), nullable=False),
        sa.Column("smtp_security", sa.String(16), nullable=False),
        sa.Column("imap_host", sa.String(255), nullable=False),
        sa.Column("imap_port", sa.Integer(), nullable=False),
        sa.Column("imap_login", sa.String(320), nullable=False),
        sa.Column("imap_password_encrypted", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "email_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(255)),
        sa.Column("subject_template", sa.Text(), nullable=False),
        sa.Column("html_template", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_email_templates_category", "email_templates", ["category"])
    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(255)),
        sa.Column("city", sa.String(255)),
        sa.Column("mailbox_id", sa.String(36), sa.ForeignKey("mail_accounts.id"), nullable=False),
        sa.Column("template_id", sa.String(36), sa.ForeignKey("email_templates.id"), nullable=False),
        sa.Column("schedule", sa.String(100)),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("run_limit", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_campaigns_category", "campaigns", ["category"])
    op.create_index("ix_campaigns_city", "campaigns", ["city"])
    op.create_table(
        "email_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("campaigns.id")),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("mailbox_id", sa.String(36), sa.ForeignKey("mail_accounts.id"), nullable=False),
        sa.Column("template_id", sa.String(36), sa.ForeignKey("email_templates.id"), nullable=False),
        sa.Column("recipient_email", sa.String(320), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("tracking_token", sa.String(64), nullable=False),
        sa.Column("message_id", sa.String(998), unique=True),
        sa.Column("error", sa.Text()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("clicked_at", sa.DateTime(timezone=True)),
        sa.Column("replied_at", sa.DateTime(timezone=True)),
        sa.Column("bounced_at", sa.DateTime(timezone=True)),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True)),
    )
    for name, columns, unique in [
        ("ix_email_deliveries_campaign_id", ["campaign_id"], False),
        ("ix_email_deliveries_company_id", ["company_id"], False),
        ("ix_email_deliveries_mailbox_id", ["mailbox_id"], False),
        ("ix_email_deliveries_recipient_email", ["recipient_email"], False),
        ("ix_email_deliveries_status", ["status"], False),
        ("ix_email_deliveries_tracking_token", ["tracking_token"], True),
        ("ix_email_deliveries_sent_at", ["sent_at"], False),
        ("uq_delivery_company_recipient", ["company_id", "recipient_email"], True),
    ]:
        op.create_index(name, "email_deliveries", columns, unique=unique)
    op.create_table(
        "tracked_links",
        sa.Column("token", sa.String(64), primary_key=True),
        sa.Column("delivery_id", sa.String(36), sa.ForeignKey("email_deliveries.id"), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
    )
    op.create_index("ix_tracked_links_delivery_id", "tracked_links", ["delivery_id"])
    op.create_table(
        "suppressions",
        sa.Column("email", sa.String(320), primary_key=True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "inbound_replies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("delivery_id", sa.String(36), sa.ForeignKey("email_deliveries.id")),
        sa.Column("mailbox_id", sa.String(36), sa.ForeignKey("mail_accounts.id"), nullable=False),
        sa.Column("message_id", sa.String(998), unique=True),
        sa.Column("sender", sa.String(320), nullable=False),
        sa.Column("subject", sa.Text()),
        sa.Column("text_body", sa.Text()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_inbound_replies_delivery_id", "inbound_replies", ["delivery_id"])
    op.create_table(
        "google_sheets_config",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("spreadsheet_id", sa.String(255), nullable=False),
        sa.Column("worksheet_name", sa.String(255), nullable=False),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "ai_config",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("api_base", sa.String(500), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "phrase_search_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("phrase", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_phrase_search_runs_status", "phrase_search_runs", ["status"])


def downgrade() -> None:
    for table in [
        "phrase_search_runs", "ai_config", "google_sheets_config", "inbound_replies",
        "suppressions", "tracked_links", "email_deliveries", "campaigns",
        "email_templates", "mail_accounts",
    ]:
        op.drop_table(table)
