"""Preserve reviewable source attribution through explicit recipe saves."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_recipe_provenance"
down_revision = "0008_mcp_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recipes",
        sa.Column(
            "source_ingredients",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("recipes", sa.Column("provenance", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("recipes", "provenance")
    op.drop_column("recipes", "source_ingredients")
