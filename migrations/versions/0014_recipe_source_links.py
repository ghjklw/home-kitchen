"""Stable provider identities for deliberately imported household recipes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014_recipe_source_links"
down_revision = "0013_plan_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recipe_source_links",
        sa.Column("provider", sa.Text, primary_key=True),
        sa.Column("external_recipe_id", sa.Text, primary_key=True),
        sa.Column(
            "recipe_id", sa.Uuid, sa.ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("metadata", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("recipe_source_links")
