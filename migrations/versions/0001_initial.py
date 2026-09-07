"""Initial parser and company storage."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_external_id", sa.String(255)),
        sa.Column("source_url", sa.Text()),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("category", sa.String(255)),
        sa.Column("city", sa.String(255)),
        sa.Column("address", sa.Text()),
        sa.Column("phone", sa.String(255)),
        sa.Column("email", sa.String(320)),
        sa.Column("website", sa.Text()),
        sa.Column("inn", sa.String(16)),
        sa.Column("contact_person", sa.String(255)),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("normalized_address", sa.Text()),
        sa.Column("normalized_phone", sa.String(32)),
        sa.Column("website_domain", sa.String(255)),
        sa.Column("name_address_fingerprint", sa.String(64)),
        sa.Column("manually_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_companies_source", "companies", ["source"])
    op.create_index("ix_companies_category", "companies", ["category"])
    op.create_index("ix_companies_city", "companies", ["city"])
    op.create_index("ix_companies_email", "companies", ["email"])
    op.create_index("uq_company_source_external", "companies", ["source", "source_external_id"], unique=True)
    op.create_index("uq_company_domain", "companies", ["website_domain"], unique=True)
    op.create_index("uq_company_phone", "companies", ["normalized_phone"], unique=True)
    op.create_index("uq_company_inn", "companies", ["inn"], unique=True)
    op.create_index("uq_company_name_address", "companies", ["name_address_fingerprint"], unique=True)

    op.create_table(
        "source_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("category", sa.String(255), nullable=False),
        sa.Column("city", sa.String(255), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("limit_new", sa.Integer(), nullable=False),
        sa.Column("schedule", sa.String(100)),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_source_jobs_source", "source_jobs", ["source"])

    op.create_table(
        "parser_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("scanned", sa.Integer(), nullable=False),
        sa.Column("inserted", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_parser_runs_job_id", "parser_runs", ["job_id"])
    op.create_index("ix_parser_runs_status", "parser_runs", ["status"])


def downgrade() -> None:
    op.drop_table("parser_runs")
    op.drop_table("source_jobs")
    op.drop_table("companies")

