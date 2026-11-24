"""Merge product enrichment and retailer product mapping migration branches."""

revision = "0035_merge_product_schema_heads"
down_revision = ("0033_product_enrichment", "0034_retailer_product_mappings")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
