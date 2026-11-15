"""Store explicit household SKU choices separately from generic foods and purchases.

Revision ID: 0011_catalogue_skus
Revises: 0010_catalogue
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_catalogue_skus"
down_revision = "0010_catalogue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_catalogue_sku_options",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("household_id", sa.Uuid, nullable=False),
        sa.Column("food_catalogue_id", sa.Uuid, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("product_id", sa.Text, nullable=False),
        sa.Column("current_snapshot_id", sa.Uuid),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "household_id",
            "food_catalogue_id",
            "provider",
            "product_id",
            name="uq_catalogue_sku_exact_product",
        ),
        sa.UniqueConstraint("id", "household_id", name="uq_catalogue_sku_id_household"),
        sa.ForeignKeyConstraint(
            ["food_catalogue_id"],
            ["kitchen_food_catalogue.id"],
            ondelete="RESTRICT",
            name="fk_catalogue_sku_food",
        ),
        sa.CheckConstraint("length(provider) > 0", name="ck_catalogue_sku_provider"),
        sa.CheckConstraint("length(product_id) > 0", name="ck_catalogue_sku_product_id"),
    )
    op.create_index(
        "ix_catalogue_sku_household_food",
        "kitchen_catalogue_sku_options",
        ["household_id", "food_catalogue_id"],
    )

    op.create_table(
        "kitchen_catalogue_sku_snapshots",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("household_id", sa.Uuid, nullable=False),
        sa.Column("option_id", sa.Uuid, nullable=False),
        sa.Column("snapshot", postgresql.JSONB, nullable=False),
        sa.Column("snapshot_ref", sa.Text),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_status", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "id", "household_id", "option_id", name="uq_catalogue_snapshot_option_scope"
        ),
        sa.ForeignKeyConstraint(
            ["option_id", "household_id"],
            ["kitchen_catalogue_sku_options.id", "kitchen_catalogue_sku_options.household_id"],
            ondelete="RESTRICT",
            name="fk_catalogue_snapshot_option_scope",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'", name="ck_catalogue_snapshot_object"
        ),
        sa.CheckConstraint(
            "evidence_status IN ('unverified', 'provider_verified')",
            name="ck_catalogue_snapshot_evidence_status",
        ),
    )
    op.create_foreign_key(
        "fk_catalogue_sku_current_snapshot_scope",
        "kitchen_catalogue_sku_options",
        "kitchen_catalogue_sku_snapshots",
        ["current_snapshot_id", "household_id", "id"],
        ["id", "household_id", "option_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "kitchen_catalogue_sku_selections",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("household_id", sa.Uuid, nullable=False),
        sa.Column("option_id", sa.Uuid, nullable=False),
        sa.Column("snapshot_id", sa.Uuid, nullable=False),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column(
            "selected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "household_id", "option_id"],
            [
                "kitchen_catalogue_sku_snapshots.id",
                "kitchen_catalogue_sku_snapshots.household_id",
                "kitchen_catalogue_sku_snapshots.option_id",
            ],
            ondelete="RESTRICT",
            name="fk_catalogue_selection_snapshot_scope",
        ),
        sa.CheckConstraint("length(actor_id) > 0", name="ck_catalogue_selection_actor"),
    )
    op.create_index(
        "ix_catalogue_sku_selections_household_time",
        "kitchen_catalogue_sku_selections",
        ["household_id", "selected_at"],
    )

    op.create_table(
        "kitchen_catalogue_sku_preferences",
        sa.Column("household_id", sa.Uuid, primary_key=True),
        sa.Column("option_id", sa.Uuid, primary_key=True),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column(
            "set_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["option_id", "household_id"],
            ["kitchen_catalogue_sku_options.id", "kitchen_catalogue_sku_options.household_id"],
            ondelete="CASCADE",
            name="fk_catalogue_preference_option_scope",
        ),
        sa.CheckConstraint("length(actor_id) > 0", name="ck_catalogue_preference_actor"),
    )
    op.create_table(
        "kitchen_catalogue_sku_preference_events",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("household_id", sa.Uuid, nullable=False),
        sa.Column("option_id", sa.Uuid, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["option_id", "household_id"],
            ["kitchen_catalogue_sku_options.id", "kitchen_catalogue_sku_options.household_id"],
            ondelete="RESTRICT",
            name="fk_catalogue_preference_event_option_scope",
        ),
        sa.CheckConstraint(
            "action IN ('set', 'unset')", name="ck_catalogue_preference_event_action"
        ),
        sa.CheckConstraint("length(actor_id) > 0", name="ck_catalogue_preference_event_actor"),
    )
    op.create_index(
        "ix_catalogue_preference_events_household_time",
        "kitchen_catalogue_sku_preference_events",
        ["household_id", "occurred_at"],
    )

    op.create_table(
        "kitchen_catalogue_sku_purchases",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("household_id", sa.Uuid, nullable=False),
        sa.Column("option_id", sa.Uuid, nullable=False),
        sa.Column("snapshot_id", sa.Uuid, nullable=False),
        sa.Column("evidence_kind", sa.Text, nullable=False),
        sa.Column("evidence_ref", sa.Text, nullable=False),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3)),
        sa.Column("quantity_unit", sa.Text),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "household_id", "option_id"],
            [
                "kitchen_catalogue_sku_snapshots.id",
                "kitchen_catalogue_sku_snapshots.household_id",
                "kitchen_catalogue_sku_snapshots.option_id",
            ],
            ondelete="RESTRICT",
            name="fk_catalogue_purchase_snapshot_scope",
        ),
        sa.CheckConstraint(
            "evidence_kind IN ('retailer_order', 'receipt', 'manual_confirmation')",
            name="ck_catalogue_purchase_evidence_kind",
        ),
        sa.CheckConstraint("length(evidence_ref) > 0", name="ck_catalogue_purchase_evidence_ref"),
        sa.CheckConstraint("length(actor_id) > 0", name="ck_catalogue_purchase_actor"),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0", name="ck_catalogue_purchase_quantity"
        ),
        sa.CheckConstraint(
            "(quantity IS NULL) = (quantity_unit IS NULL)",
            name="ck_catalogue_purchase_quantity_unit_pair",
        ),
    )
    op.create_index(
        "ix_catalogue_sku_purchases_household_time",
        "kitchen_catalogue_sku_purchases",
        ["household_id", "purchased_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_catalogue_sku_purchases_household_time", table_name="kitchen_catalogue_sku_purchases"
    )
    op.drop_table("kitchen_catalogue_sku_purchases")
    op.drop_index(
        "ix_catalogue_preference_events_household_time",
        table_name="kitchen_catalogue_sku_preference_events",
    )
    op.drop_table("kitchen_catalogue_sku_preference_events")
    op.drop_table("kitchen_catalogue_sku_preferences")
    op.drop_index(
        "ix_catalogue_sku_selections_household_time", table_name="kitchen_catalogue_sku_selections"
    )
    op.drop_table("kitchen_catalogue_sku_selections")
    op.drop_constraint(
        "fk_catalogue_sku_current_snapshot_scope",
        "kitchen_catalogue_sku_options",
        type_="foreignkey",
    )
    op.drop_table("kitchen_catalogue_sku_snapshots")
    op.drop_index("ix_catalogue_sku_household_food", table_name="kitchen_catalogue_sku_options")
    op.drop_table("kitchen_catalogue_sku_options")
