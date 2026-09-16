"""Per-direction email attachments and editable draft recipient."""

from alembic import op
import sqlalchemy as sa


revision = "0017_direction_attachments"
down_revision = "0016_admin_self_service"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "direction_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("storage_name", sa.String(255), nullable=False, unique=True),
        sa.Column("content_type", sa.String(150), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("direction_id", "sha256", name="uq_direction_attachment_sha"),
    )
    op.create_index("ix_direction_attachments_direction_id", "direction_attachments", ["direction_id"])
    op.create_index("ix_direction_attachments_sha256", "direction_attachments", ["sha256"])
    op.create_index("ix_direction_attachments_active", "direction_attachments", ["active"])
    op.add_column("email_deliveries", sa.Column("attachments_snapshot", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("sheet_personalization_drafts", sa.Column("recipient_email", sa.String(320), nullable=True))
    op.add_column("sheet_personalization_drafts", sa.Column("attachments_snapshot", sa.JSON(), nullable=False, server_default="[]"))
    op.execute("""
        UPDATE sheet_personalization_drafts d
        SET recipient_email=COALESCE(c.decision_maker_email, c.company_email, c.email)
        FROM companies c WHERE c.id=d.company_id
    """)


def downgrade() -> None:
    op.drop_column("sheet_personalization_drafts", "attachments_snapshot")
    op.drop_column("sheet_personalization_drafts", "recipient_email")
    op.drop_column("email_deliveries", "attachments_snapshot")
    op.drop_index("ix_direction_attachments_active", table_name="direction_attachments")
    op.drop_index("ix_direction_attachments_sha256", table_name="direction_attachments")
    op.drop_index("ix_direction_attachments_direction_id", table_name="direction_attachments")
    op.drop_table("direction_attachments")
