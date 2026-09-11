"""Add safe Google Sheets personalization drafts and send idempotency."""

from alembic import op
import sqlalchemy as sa


revision = "0010_sheet_ai_trigger"
down_revision = "0009_deepseek_website_ai"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("email_deliveries", sa.Column("idempotency_key", sa.String(255)))
    op.create_index("ix_email_deliveries_idempotency_key", "email_deliveries", ["idempotency_key"], unique=True)
    op.create_table(
        "sheet_personalization_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("config_id", sa.String(36), sa.ForeignKey("google_sheets_config.id", ondelete="CASCADE"), nullable=False),
        sa.Column("template_id", sa.String(36), sa.ForeignKey("email_templates.id"), nullable=False),
        sa.Column("mailbox_id", sa.String(36), sa.ForeignKey("mail_accounts.id")),
        sa.Column("command_key", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False, server_default="preparing"),
        sa.Column("subject", sa.Text()),
        sa.Column("html_body", sa.Text()),
        sa.Column("text_body", sa.Text()),
        sa.Column("facts", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("delivery_id", sa.String(36), sa.ForeignKey("email_deliveries.id", ondelete="SET NULL"), unique=True),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("company_id", "direction_id", "command_key", "request_key", "status", "created_at"):
        op.create_index(f"ix_sheet_personalization_drafts_{column}", "sheet_personalization_drafts", [column], unique=column == "command_key")


def downgrade() -> None:
    op.drop_table("sheet_personalization_drafts")
    op.drop_index("ix_email_deliveries_idempotency_key", table_name="email_deliveries")
    op.drop_column("email_deliveries", "idempotency_key")
