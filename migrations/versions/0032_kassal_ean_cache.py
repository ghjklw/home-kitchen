"""Persist Kassal.app EAN lookup results across syncs and pod restarts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032_kassal_ean_cache"
down_revision = "0031_alias_household_source_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kassal_ean_cache",
        sa.Column("ean", sa.String(64), primary_key=True),
        sa.Column("found", sa.Boolean(), nullable=False),
        sa.Column(
            "product_payload",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("kassal_ean_cache")
