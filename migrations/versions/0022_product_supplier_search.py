"""Isolated supplier/product web-search workspace.

Revision ID: 0022_product_supplier_search
Revises: 0021_direction_region_scope
"""
from alembic import op
import sqlalchemy as sa


revision = "0022_product_supplier_search"
down_revision = "0021_direction_region_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_search_sheet_config",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("spreadsheet_id", sa.String(length=255)),
        sa.Column("worksheet_name", sa.String(length=255), nullable=False, server_default="Поиск товаров"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "product_search_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("city", sa.String(length=255)),
        sa.Column("region", sa.String(length=255)),
        sa.Column("limit", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("use_ai", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("urls_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_product_search_runs_city", "product_search_runs", ["city"])
    op.create_index("ix_product_search_runs_region", "product_search_runs", ["region"])
    op.create_index("ix_product_search_runs_status", "product_search_runs", ["status"])
    op.create_table(
        "product_search_results",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("product_search_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("product_url", sa.Text()),
        sa.Column("company_name", sa.Text()),
        sa.Column("region", sa.String(length=255)),
        sa.Column("city", sa.String(length=255)),
        sa.Column("website", sa.Text()),
        sa.Column("email", sa.String(length=320)),
        sa.Column("phone", sa.String(length=255)),
        sa.Column("product_name", sa.Text()),
        sa.Column("price", sa.String(length=255)),
        sa.Column("extraction_method", sa.String(length=32), nullable=False, server_default="deterministic"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="found"),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("sheet_row", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "source_url", name="uq_product_result_run_url"),
    )
    op.create_index("ix_product_search_results_run_id", "product_search_results", ["run_id"])
    op.create_index("ix_product_search_results_email", "product_search_results", ["email"])
    op.create_index("ix_product_search_results_status", "product_search_results", ["status"])
    op.create_index("ix_product_search_results_selected", "product_search_results", ["selected"])
    op.create_table(
        "product_search_deliveries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("result_id", sa.String(length=36), sa.ForeignKey("product_search_results.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mailbox_id", sa.String(length=36), sa.ForeignKey("mail_accounts.id"), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("message_id", sa.String(length=998), unique=True),
        sa.Column("error", sa.Text()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("result_id", "recipient_email", name="uq_product_delivery_recipient"),
    )
    op.create_index("ix_product_search_deliveries_result_id", "product_search_deliveries", ["result_id"])
    op.create_index("ix_product_search_deliveries_mailbox_id", "product_search_deliveries", ["mailbox_id"])
    op.create_index("ix_product_search_deliveries_recipient_email", "product_search_deliveries", ["recipient_email"])
    op.create_index("ix_product_search_deliveries_status", "product_search_deliveries", ["status"])
    op.create_index("ix_product_search_deliveries_sent_at", "product_search_deliveries", ["sent_at"])


def downgrade() -> None:
    op.drop_table("product_search_deliveries")
    op.drop_table("product_search_results")
    op.drop_table("product_search_runs")
    op.drop_table("product_search_sheet_config")
