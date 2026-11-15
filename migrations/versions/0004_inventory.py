"""Separate available household stock from gross shopping requirements."""

import sqlalchemy as sa
from alembic import op

revision = "0004_inventory"
down_revision = "0003_basket"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("items", "regulars"):
        op.add_column(table, sa.Column("category", sa.Text, nullable=False, server_default="Other"))
    op.create_table(
        "kitchen_inventory",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("normalized_name", sa.Text, nullable=False),
        sa.Column("unit", sa.Text, nullable=False),
        sa.Column("normalized_unit", sa.Text, nullable=False),
        sa.Column("quantity", sa.Numeric, nullable=False),
        sa.Column("category", sa.Text, nullable=False, server_default="Other"),
        sa.Column("note", sa.Text),
        sa.UniqueConstraint("normalized_name", "normalized_unit"),
        sa.CheckConstraint("quantity >= 0", name="inventory_nonnegative_quantity"),
    )


def downgrade() -> None:
    op.drop_table("kitchen_inventory")
    for table in ("regulars", "items"):
        op.drop_column(table, "category")
