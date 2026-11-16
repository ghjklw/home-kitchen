"""Merge the product preference and recipe translation migration branches."""

revision = "0024_merge_cat_translations"
down_revision = ("0023_product_preferences", "0023_recipe_translations")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
