"""Add direction proposal templates and editable proposal drafts."""

from alembic import op
import sqlalchemy as sa


revision = "0012_proposal_drafts"
down_revision = "0011_phrase_search_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("sheet_personalization_drafts", "config_id", existing_type=sa.String(36), nullable=True)
    op.create_table(
        "direction_proposal_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("direction_id", sa.String(36), sa.ForeignKey("directions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("greeting", sa.Text(), nullable=False),
        sa.Column("main_body", sa.Text(), nullable=False),
        sa.Column("extra_block", sa.Text(), nullable=False, server_default=""),
        sa.Column("cta", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("ai_instruction", sa.Text(), nullable=False),
        sa.Column("ai_personalization_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("direction_id", name="uq_direction_proposal_template_direction"),
    )
    op.create_index("ix_direction_proposal_templates_direction_id", "direction_proposal_templates", ["direction_id"])
    columns = [
        sa.Column("proposal_template_id", sa.String(36), sa.ForeignKey("direction_proposal_templates.id", ondelete="SET NULL")),
        sa.Column("template_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("greeting", sa.Text()),
        sa.Column("main_body", sa.Text()),
        sa.Column("ai_personalization", sa.Text()),
        sa.Column("extra_block", sa.Text()),
        sa.Column("cta", sa.Text()),
        sa.Column("signature", sa.Text()),
        sa.Column("ai_evidence", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("ai_response_data", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("html_snapshot", sa.Text()),
        sa.Column("sent_html_snapshot", sa.Text()),
        sa.Column("sent_text_snapshot", sa.Text()),
    ]
    for column in columns:
        op.add_column("sheet_personalization_drafts", column)
    op.create_index("ix_sheet_personalization_drafts_proposal_template_id", "sheet_personalization_drafts", ["proposal_template_id"])


def downgrade() -> None:
    op.drop_index("ix_sheet_personalization_drafts_proposal_template_id", table_name="sheet_personalization_drafts")
    for column in (
        "sent_text_snapshot", "sent_html_snapshot", "html_snapshot", "ai_response_data", "ai_evidence",
        "signature", "cta", "extra_block", "ai_personalization", "main_body", "greeting",
        "template_version", "proposal_template_id",
    ):
        op.drop_column("sheet_personalization_drafts", column)
    op.drop_table("direction_proposal_templates")
    op.alter_column("sheet_personalization_drafts", "config_id", existing_type=sa.String(36), nullable=False)
