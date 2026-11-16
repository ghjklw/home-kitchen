"""Add reusable canonical product and unit identities with scoped aliases."""

import sqlalchemy as sa
from alembic import op

revision = "0022_product_unit_identity"
down_revision = "0021_nullable_source_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_canonical_products",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("canonical_name", "variant", name="uq_canonical_product_identity"),
        sa.CheckConstraint("length(trim(canonical_name)) > 0", name="ck_canonical_product_name"),
    )
    op.create_table(
        "kitchen_canonical_units",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("canonical_name", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("length(trim(canonical_name)) > 0", name="ck_canonical_unit_name"),
    )
    op.create_table(
        "kitchen_product_aliases",
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False, server_default=""),
        sa.Column("normalized_alias", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column(
            "product_id",
            sa.Uuid(),
            sa.ForeignKey("kitchen_canonical_products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("locale", "context", "normalized_alias", "product_id"),
    )
    op.create_index(
        "ix_product_alias_lookup",
        "kitchen_product_aliases",
        ["locale", "context", "normalized_alias"],
    )
    op.create_table(
        "kitchen_unit_aliases",
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False, server_default=""),
        sa.Column("normalized_alias", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column(
            "unit_id",
            sa.Uuid(),
            sa.ForeignKey("kitchen_canonical_units.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("locale", "context", "normalized_alias", "unit_id"),
    )
    op.create_index(
        "ix_unit_alias_lookup", "kitchen_unit_aliases", ["locale", "context", "normalized_alias"]
    )


def downgrade() -> None:
    op.drop_table("kitchen_unit_aliases")
    op.drop_table("kitchen_product_aliases")
    op.drop_table("kitchen_canonical_units")
    op.drop_table("kitchen_canonical_products")
