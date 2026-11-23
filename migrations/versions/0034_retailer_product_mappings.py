"""Persist reviewed retailer-to-canonical product mappings and AI suggestions."""

import sqlalchemy as sa
from alembic import op

revision = "0034_retailer_product_mappings"
down_revision = "0033_meal_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_retailer_product_mappings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("retailer", sa.Text(), nullable=False),
        sa.Column("retailer_product_id", sa.Text(), nullable=True),
        sa.Column("ean", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("normalized_description", sa.Text(), nullable=False),
        sa.Column("description_hash", sa.String(64), nullable=False),
        sa.Column(
            "canonical_product_id",
            sa.Uuid(),
            sa.ForeignKey("kitchen_canonical_products.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "suggested_product_id",
            sa.Uuid(),
            sa.ForeignKey("kitchen_canonical_products.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("suggested_product_name", sa.Text(), nullable=True),
        sa.Column("suggested_category", sa.Text(), nullable=True),
        sa.Column("product_evidence", sa.Text(), nullable=True),
        sa.Column("category_evidence", sa.Text(), nullable=True),
        sa.Column("mapping_source", sa.Text(), nullable=True),
        sa.Column("mapping_confidence", sa.Float(), nullable=True),
        sa.Column("review_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("category_source", sa.Text(), nullable=True),
        sa.Column("category_confidence", sa.Float(), nullable=True),
        sa.Column("category_review_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("package_size", sa.Numeric(18, 6), nullable=True),
        sa.Column("package_unit", sa.Text(), nullable=True),
        sa.Column("package_inference_source", sa.Text(), nullable=True),
        sa.Column("package_confidence", sa.Float(), nullable=True),
        sa.Column("package_evidence", sa.Text(), nullable=True),
        sa.Column("apply_to_similar", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "review_status IN ('pending', 'confirmed', 'ignored')",
            name="ck_retailer_product_mapping_review_status",
        ),
        sa.CheckConstraint(
            "category_review_status IN ('pending', 'confirmed', 'ignored')",
            name="ck_retailer_product_mapping_category_status",
        ),
        sa.CheckConstraint(
            "mapping_confidence IS NULL OR (mapping_confidence >= 0 AND mapping_confidence <= 1)",
            name="ck_retailer_product_mapping_confidence",
        ),
        sa.CheckConstraint(
            "category_confidence IS NULL OR (category_confidence >= 0 AND category_confidence <= 1)",
            name="ck_retailer_product_category_confidence",
        ),
        sa.CheckConstraint(
            "package_confidence IS NULL OR (package_confidence >= 0 AND package_confidence <= 1)",
            name="ck_retailer_product_package_confidence",
        ),
        sa.CheckConstraint(
            "package_size IS NULL OR package_size > 0",
            name="ck_retailer_product_package_size",
        ),
        sa.CheckConstraint("length(trim(retailer)) > 0", name="ck_retailer_product_retailer"),
        sa.CheckConstraint("length(trim(description)) > 0", name="ck_retailer_product_description"),
        sa.UniqueConstraint(
            "retailer", "description_hash", name="uq_retailer_product_mapping_description"
        ),
    )
    op.create_index(
        "ix_retailer_product_mapping_identifier",
        "kitchen_retailer_product_mappings",
        ["retailer", "retailer_product_id"],
    )
    op.create_index(
        "ix_retailer_product_mapping_ean",
        "kitchen_retailer_product_mappings",
        ["ean"],
    )
    op.create_index(
        "ix_retailer_product_mapping_global_description",
        "kitchen_retailer_product_mappings",
        ["description_hash", "apply_to_similar", "review_status"],
    )

    op.create_table(
        "kitchen_retailer_product_mapping_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "mapping_id",
            sa.Uuid(),
            sa.ForeignKey("kitchen_retailer_product_mappings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("previous_state", sa.Text(), nullable=False),
        sa.Column("new_state", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_retailer_product_mapping_events_mapping_occurred",
        "kitchen_retailer_product_mapping_events",
        ["mapping_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_retailer_product_mapping_events_mapping_occurred",
        table_name="kitchen_retailer_product_mapping_events",
    )
    op.drop_table("kitchen_retailer_product_mapping_events")
    op.drop_index(
        "ix_retailer_product_mapping_global_description",
        table_name="kitchen_retailer_product_mappings",
    )
    op.drop_index("ix_retailer_product_mapping_ean", table_name="kitchen_retailer_product_mappings")
    op.drop_index(
        "ix_retailer_product_mapping_identifier",
        table_name="kitchen_retailer_product_mappings",
    )
    op.drop_table("kitchen_retailer_product_mappings")
