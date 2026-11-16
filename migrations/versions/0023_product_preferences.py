"""Add household preferred and excluded Oda SKUs per canonical product.

Revision ID: 0023_product_preferences
Revises: 0022_product_unit_identity
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_product_preferences"
down_revision = "0022_product_unit_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_canonical_product_oda_preferences",
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column(
            "product_id",
            sa.Uuid(),
            sa.ForeignKey("kitchen_canonical_products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("oda_product_id", sa.Text(), nullable=False),
        sa.Column("preference_order", sa.Integer(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("household_id", "product_id", "oda_product_id"),
    )
    op.create_index(
        "ix_product_oda_preferences_lookup",
        "kitchen_canonical_product_oda_preferences",
        ["household_id", "product_id", "preference_order"],
    )

    op.create_table(
        "kitchen_canonical_product_oda_exclusions",
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column(
            "product_id",
            sa.Uuid(),
            sa.ForeignKey("kitchen_canonical_products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("oda_product_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "payload",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("household_id", "product_id", "oda_product_id"),
    )
    op.create_index(
        "ix_product_oda_exclusions_lookup",
        "kitchen_canonical_product_oda_exclusions",
        ["household_id", "product_id"],
    )


def downgrade() -> None:
    op.drop_table("kitchen_canonical_product_oda_exclusions")
    op.drop_table("kitchen_canonical_product_oda_preferences")
