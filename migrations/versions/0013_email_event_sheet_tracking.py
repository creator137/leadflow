"""Persist email event history and expose current state to Google Sheets."""

from alembic import op
import sqlalchemy as sa


revision = "0013_email_event_sheet_tracking"
down_revision = "0012_proposal_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("delivery_id", sa.String(36), sa.ForeignKey("email_deliveries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_email_events_delivery_id", "email_events", ["delivery_id"])
    op.create_index("ix_email_events_company_id", "email_events", ["company_id"])
    op.create_index("ix_email_events_event_type", "email_events", ["event_type"])
    op.create_index("ix_email_events_occurred_at", "email_events", ["occurred_at"])


def downgrade() -> None:
    op.drop_table("email_events")
