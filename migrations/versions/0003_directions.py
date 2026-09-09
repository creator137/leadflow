"""Directions, business fields, enrichment provenance and sheet row identity."""

from alembic import op
import sqlalchemy as sa

revision = "0003_directions"
down_revision = "0002_outreach"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, column in [
        ("region", sa.Column("region", sa.String(255))),
        ("company_email", sa.Column("company_email", sa.String(320))),
        ("company_phone", sa.Column("company_phone", sa.String(255))),
        ("branches_count", sa.Column("branches_count", sa.Integer())),
        ("decision_maker_name", sa.Column("decision_maker_name", sa.String(255))),
        ("decision_maker_position", sa.Column("decision_maker_position", sa.String(255))),
        ("decision_maker_email", sa.Column("decision_maker_email", sa.String(320))),
        ("decision_maker_phone", sa.Column("decision_maker_phone", sa.String(255))),
        ("communication_started_at", sa.Column("communication_started_at", sa.DateTime(timezone=True))),
        ("action", sa.Column("action", sa.Text())),
        ("result", sa.Column("result", sa.Text())),
        ("created_at", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)),
        ("updated_at", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)),
    ]:
        op.add_column("companies", column)
    op.execute("UPDATE companies SET company_email = email, company_phone = phone, decision_maker_name = contact_person, created_at = collected_at, updated_at = collected_at")
    op.create_index("ix_companies_region", "companies", ["region"])
    op.create_index("ix_companies_company_email", "companies", ["company_email"])

    op.create_table(
        "directions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("sheet_tab", sa.String(100), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("limit_new", sa.Integer(), nullable=False),
        sa.Column("schedule", sa.String(100)),
        sa.Column("email_enrichment_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_enrichment_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_directions_active", "directions", ["active"])
    op.create_table(
        "direction_search_queries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("query", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("direction_id", "query", name="uq_direction_query"),
    )
    op.create_index("ix_direction_search_queries_direction_id", "direction_search_queries", ["direction_id"])
    op.create_table(
        "direction_locations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("city", sa.String(255), nullable=False),
        sa.Column("region", sa.String(255)),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("direction_id", "city", "region", name="uq_direction_location"),
    )
    op.create_index("ix_direction_locations_direction_id", "direction_locations", ["direction_id"])
    op.create_table(
        "direction_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("direction_id", "source", name="uq_direction_source"),
    )
    op.create_index("ix_direction_sources_direction_id", "direction_sources", ["direction_id"])
    op.create_table(
        "company_directions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "direction_id", name="uq_company_direction"),
    )
    op.create_index("ix_company_directions_company_id", "company_directions", ["company_id"])
    op.create_index("ix_company_directions_direction_id", "company_directions", ["direction_id"])
    op.create_table(
        "company_source_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_external_id", sa.String(255)),
        sa.Column("source_url", sa.Text()),
        sa.Column("query", sa.String(255)),
        sa.Column("city", sa.String(255)),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "source_external_id", name="uq_company_source_record_external"),
    )
    op.create_index("ix_company_source_records_company_id", "company_source_records", ["company_id"])
    op.create_table(
        "company_field_provenance",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field", sa.String(100), nullable=False),
        sa.Column("value", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("confidence", sa.Float()),
        sa.Column("discovery_method", sa.String(64), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_company_field_provenance_company_id", "company_field_provenance", ["company_id"])
    op.create_index("ix_company_field_provenance_field", "company_field_provenance", ["field"])
    op.create_table(
        "sheet_row_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("spreadsheet_id", sa.String(255), nullable=False),
        sa.Column("sheet_tab", sa.String(100), nullable=False),
        sa.Column("sheet_row", sa.Integer(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "direction_id", "spreadsheet_id", name="uq_sheet_company_direction"),
        sa.UniqueConstraint("spreadsheet_id", "sheet_tab", "sheet_row", name="uq_sheet_row"),
    )
    op.create_index("ix_sheet_row_mappings_company_id", "sheet_row_mappings", ["company_id"])
    op.create_index("ix_sheet_row_mappings_direction_id", "sheet_row_mappings", ["direction_id"])
    op.create_table(
        "direction_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("limit_new", sa.Integer(), nullable=False),
        sa.Column("scanned", sa.Integer(), nullable=False),
        sa.Column("inserted", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_direction_runs_direction_id", "direction_runs", ["direction_id"])
    op.create_index("ix_direction_runs_status", "direction_runs", ["status"])


def downgrade() -> None:
    for table in [
        "direction_runs", "sheet_row_mappings", "company_field_provenance", "company_source_records",
        "company_directions", "direction_sources", "direction_locations", "direction_search_queries", "directions",
    ]:
        op.drop_table(table)
    op.drop_index("ix_companies_company_email", table_name="companies")
    op.drop_index("ix_companies_region", table_name="companies")
    for column in [
        "updated_at", "created_at", "result", "action", "communication_started_at", "decision_maker_phone",
        "decision_maker_email", "decision_maker_position", "decision_maker_name", "branches_count", "company_phone",
        "company_email", "region",
    ]:
        op.drop_column("companies", column)
