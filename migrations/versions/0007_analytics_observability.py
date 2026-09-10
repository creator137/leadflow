"""Add factual search observations and Google sync history."""

from alembic import op
import sqlalchemy as sa

revision = "0007_analytics_observability"
down_revision = "0006_outbound_threading"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("direction_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id", ondelete="SET NULL")),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("query", sa.String(255), nullable=False),
        sa.Column("city", sa.String(255), nullable=False),
        sa.Column("source_external_id", sa.String(255)),
        sa.Column("source_url", sa.Text()),
        sa.Column("is_new", sa.Boolean(), nullable=False),
        sa.Column("matched_by", sa.String(64)),
        sa.Column("has_phone", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_email", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_website", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_branches_count", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_type", sa.String(255)),
        sa.Column("error_message", sa.Text()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("run_id", "direction_id", "company_id", "source", "query", "city", "is_new", "observed_at"):
        op.create_index(f"ix_search_observations_{column}", "search_observations", [column])

    op.create_table(
        "google_sync_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("config_id", sa.String(36), sa.ForeignKey("google_sheets_config.id", ondelete="CASCADE"), nullable=False),
        sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("rows_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("manual_changes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conflicts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(255)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    for column in ("config_id", "direction_id", "status", "started_at"):
        op.create_index(f"ix_google_sync_runs_{column}", "google_sync_runs", [column])


def downgrade() -> None:
    op.drop_table("google_sync_runs")
    op.drop_table("search_observations")
