"""Store retailer-neutral product enrichment, QA and user overrides separately."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033_product_enrichment"
down_revision = "0032_kassal_ean_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(none_as_null=True), "postgresql"
    )
    op.create_table(
        "catalogue_product_enrichment",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("mapping_key", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("source", json_type, nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result", json_type),
        sa.Column("override", json_type),
        sa.Column("override_reason", sa.Text()),
        sa.Column("override_actor", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "household_id", "mapping_key", name="uq_product_enrichment_household_key"
        ),
    )
    op.create_table(
        "catalogue_product_enrichment_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_product_enrichment_household_status",
        "catalogue_product_enrichment",
        ["household_id", "status"],
    )
    op.create_index(
        "ix_product_enrichment_event_product",
        "catalogue_product_enrichment_events",
        ["household_id", "product_id"],
    )


def downgrade() -> None:
    op.drop_table("catalogue_product_enrichment_events")
    op.drop_table("catalogue_product_enrichment")
