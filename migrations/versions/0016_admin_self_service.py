"""Admin self-service settings and campaign observability."""

from alembic import op
import sqlalchemy as sa


revision = "0016_admin_self_service"
down_revision = "0015_email_deliverability"
branch_labels = None
depends_on = None


DEFAULT_SIGNATURE = """С уважением,
Агеева Юлия
специалист по развитию
+7 916 208-66-28
zakaz-1.1@bogp.ru
www.bogorodsk-pryanik.ru"""


def upgrade() -> None:
    op.add_column("directions", sa.Column("automatic_template_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_directions_automatic_template", "directions", "email_templates",
        ["automatic_template_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_directions_automatic_template_id", "directions", ["automatic_template_id"])
    op.add_column("campaigns", sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("last_queued", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("campaigns", sa.Column("last_error", sa.Text(), nullable=True))
    op.create_index("ix_campaigns_last_run_at", "campaigns", ["last_run_at"])
    op.create_table(
        "sender_settings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("position", sa.String(255), nullable=True),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(100), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("product_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("logo_path", sa.String(255), nullable=False, server_default="logo.jpg"),
        sa.Column("signature_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute(sa.text("""
        INSERT INTO sender_settings
            (id, display_name, position, company_name, phone, email, website, product_description, logo_path, signature_text)
        VALUES
            ('default', 'Агеева Юлия', 'специалист по развитию', 'Богородский пряник',
             '+7 916 208-66-28', 'zakaz-1.1@bogp.ru', 'https://www.bogorodsk-pryanik.ru',
             'Брендированные пряники и подарочные наборы с индивидуальной формой, рисунком или надписью. Доставка по России.',
             'logo.jpg', :signature)
    """).bindparams(signature=DEFAULT_SIGNATURE))
    # Existing standard signatures become inherited defaults. Any customized
    # direction signature remains an explicit override.
    op.execute(sa.text(
        "UPDATE direction_proposal_templates SET signature='' WHERE signature=:signature"
    ).bindparams(signature=DEFAULT_SIGNATURE))


def downgrade() -> None:
    op.drop_table("sender_settings")
    op.drop_index("ix_campaigns_last_run_at", table_name="campaigns")
    op.drop_column("campaigns", "last_error")
    op.drop_column("campaigns", "last_queued")
    op.drop_column("campaigns", "last_run_at")
    op.drop_index("ix_directions_automatic_template_id", table_name="directions")
    op.drop_constraint("fk_directions_automatic_template", "directions", type_="foreignkey")
    op.drop_column("directions", "automatic_template_id")
