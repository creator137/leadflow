"""Add business phrase-search journal for free URL discovery."""

from alembic import op
import sqlalchemy as sa


revision = "0011_phrase_search_actions"
down_revision = "0010_sheet_ai_trigger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("phrase_search_runs", sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="SET NULL")))
    op.add_column("phrase_search_runs", sa.Column("city", sa.String(255)))
    op.add_column("phrase_search_runs", sa.Column("region", sa.String(255)))
    op.add_column("phrase_search_runs", sa.Column("use_ai", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("phrase_search_runs", sa.Column("urls_discovered", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("phrase_search_runs", sa.Column("new_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("phrase_search_runs", sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_phrase_search_runs_direction_id", "phrase_search_runs", ["direction_id"])
    op.create_index("ix_phrase_search_runs_city", "phrase_search_runs", ["city"])
    op.create_table(
        "phrase_search_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("phrase_search_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id", ondelete="SET NULL")),
        sa.Column("phrase", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text()),
        sa.Column("website", sa.Text()),
        sa.Column("email", sa.String(320)),
        sa.Column("phone", sa.String(255)),
        sa.Column("extraction_method", sa.String(32), nullable=False, server_default="deterministic"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "source_url", name="uq_phrase_result_run_url"),
    )
    for column in ("run_id", "company_id", "status", "created_at"):
        op.create_index(f"ix_phrase_search_results_{column}", "phrase_search_results", [column])


def downgrade() -> None:
    op.drop_table("phrase_search_results")
    op.drop_index("ix_phrase_search_runs_city", table_name="phrase_search_runs")
    op.drop_index("ix_phrase_search_runs_direction_id", table_name="phrase_search_runs")
    for column in ("duplicate_count", "new_count", "urls_discovered", "use_ai", "region", "city", "direction_id"):
        op.drop_column("phrase_search_runs", column)
