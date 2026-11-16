"""Store and version recipe translations and record source language."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_recipe_translations"
down_revision = "0022_product_unit_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recipes",
        sa.Column("source_language", sa.Text(), nullable=False, server_default="und"),
    )
    op.create_table(
        "kitchen_recipe_translations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "recipe_id",
            sa.Uuid(),
            sa.ForeignKey("recipes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_locale", sa.Text(), nullable=False),
        sa.Column("recipe_version", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("instructions", postgresql.JSONB, nullable=False),
        sa.Column("ingredients", postgresql.JSONB, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "recipe_id", "target_locale", "recipe_version", name="uq_recipe_translation_version"
        ),
    )
    op.create_index(
        "ix_recipe_translations_lookup",
        "kitchen_recipe_translations",
        ["recipe_id", "target_locale"],
    )


def downgrade() -> None:
    op.drop_index("ix_recipe_translations_lookup", table_name="kitchen_recipe_translations")
    op.drop_table("kitchen_recipe_translations")
    op.drop_column("recipes", "source_language")
