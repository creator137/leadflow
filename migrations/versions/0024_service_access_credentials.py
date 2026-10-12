"""Store encrypted credentials for services listed in Help.

Revision ID: 0024_service_access_credentials
Revises: 0023_moscow_schedule_presets
"""
from alembic import op
import sqlalchemy as sa


revision = "0024_service_access_credentials"
down_revision = "0023_moscow_schedule_presets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_access_credentials",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("login_encrypted", sa.Text()),
        sa.Column("password_encrypted", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("slug", name="uq_service_access_credentials_slug"),
    )
    op.create_index("ix_service_access_credentials_slug", "service_access_credentials", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_table("service_access_credentials")
