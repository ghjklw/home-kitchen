"""Add durable weekly product selections for basket review.

Revision ID: 0003_basket
Revises: 0002_history
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_basket"
down_revision = "0002_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_basket_selections",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("week_start", sa.Date, nullable=False),
        sa.Column("week_end", sa.Date, nullable=False),
        sa.Column("need_id", sa.Uuid, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("product_id", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "week_start",
            "week_end",
            "need_id",
            "provider",
            "product_id",
            name="uq_kitchen_basket_week_need_product",
        ),
        sa.CheckConstraint("week_end >= week_start", name="ck_kitchen_basket_week_range"),
    )
    op.create_index(
        "ix_kitchen_basket_selections_week",
        "kitchen_basket_selections",
        ["week_start", "week_end"],
    )


def downgrade() -> None:
    op.drop_index("ix_kitchen_basket_selections_week", table_name="kitchen_basket_selections")
    op.drop_table("kitchen_basket_selections")
