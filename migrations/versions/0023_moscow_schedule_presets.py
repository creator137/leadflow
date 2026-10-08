"""Keep schedule presets aligned with their Moscow-time labels.

Revision ID: 0023_moscow_schedule_presets
Revises: 0022_product_supplier_search
"""
from alembic import op


revision = "0023_moscow_schedule_presets"
down_revision = "0022_product_supplier_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE directions SET schedule = '0 7 * * *' WHERE schedule = '0 5 * * *'")
    op.execute("UPDATE directions SET schedule = '0 7 * * 1-5' WHERE schedule = '0 5 * * 1-5'")
    op.execute("UPDATE campaigns SET schedule = '0 8 * * *' WHERE schedule = '0 6 * * *'")
    op.execute("UPDATE campaigns SET schedule = '0 8 * * 1-5' WHERE schedule = '0 6 * * 1-5'")


def downgrade() -> None:
    op.execute("UPDATE campaigns SET schedule = '0 6 * * *' WHERE schedule = '0 8 * * *'")
    op.execute("UPDATE campaigns SET schedule = '0 6 * * 1-5' WHERE schedule = '0 8 * * 1-5'")
    op.execute("UPDATE directions SET schedule = '0 5 * * *' WHERE schedule = '0 7 * * *'")
    op.execute("UPDATE directions SET schedule = '0 5 * * 1-5' WHERE schedule = '0 7 * * 1-5'")
