"""Add cached website analysis, DeepSeek settings and usage accounting."""

from alembic import op
import sqlalchemy as sa


revision = "0009_deepseek_website_ai"
down_revision = "0008_sheet_sync_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_settings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("enrichment_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("personalization_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("daily_request_limit", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "website_analyses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("website", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("raw_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cleaned_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("relevant_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_blocks", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("facts", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("deterministic_fields", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("ai_enrichment_result", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_website_analyses_company_id", "website_analyses", ["company_id"], unique=True)
    op.create_index("ix_website_analyses_content_hash", "website_analyses", ["content_hash"])
    op.create_index("ix_website_analyses_analyzed_at", "website_analyses", ["analyzed_at"])
    op.create_table(
        "ai_request_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("operation", sa.String(40), nullable=False),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id", ondelete="SET NULL")),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("missing_fields", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("response_data", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost", sa.Float(), nullable=False, server_default="0"),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("operation", "company_id", "request_key", "content_hash", "success", "cache_hit", "created_at"):
        op.create_index(f"ix_ai_request_logs_{column}", "ai_request_logs", [column])


def downgrade() -> None:
    op.drop_table("ai_request_logs")
    op.drop_table("website_analyses")
    op.drop_table("ai_settings")
