"""Create normalized kitchen records.

Revision ID: 0001_core
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recipes",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("position", sa.BigInteger, sa.Identity(), nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("servings", sa.Integer, nullable=False),
        sa.Column("ingredients", postgresql.JSONB, nullable=False),
        sa.Column("instructions", postgresql.JSONB, nullable=False),
        sa.Column("source_url", sa.Text),
        sa.Column("image_url", sa.Text),
        sa.Column("source", sa.Text, nullable=False),
    )
    op.create_table(
        "meals",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("position", sa.BigInteger, sa.Identity(), nullable=False, unique=True),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("slot", sa.Text, nullable=False),
        sa.Column("recipe_id", sa.Uuid, sa.ForeignKey("recipes.id"), nullable=False),
        sa.Column("servings", sa.Integer, nullable=False),
        sa.UniqueConstraint("date", "slot"),
    )
    op.create_table(
        "items",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("position", sa.BigInteger, sa.Identity(), nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("quantity", sa.Numeric, nullable=False),
        sa.Column("unit", sa.Text, nullable=False),
        sa.Column("completed", sa.Boolean, nullable=False),
    )
    op.create_table(
        "regulars",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("position", sa.BigInteger, sa.Identity(), nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("quantity", sa.Numeric, nullable=False),
        sa.Column("unit", sa.Text, nullable=False),
        sa.Column("interval_days", sa.Integer, nullable=False),
        sa.Column("last_purchased", sa.Date),
    )
    op.create_table(
        "confirmations",
        sa.Column(
            "regular_id",
            sa.Uuid,
            sa.ForeignKey("regulars.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "item_id", sa.Uuid, sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False
        ),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )


def downgrade() -> None:
    for name in ("settings", "confirmations", "regulars", "items", "meals", "recipes"):
        op.drop_table(name)
