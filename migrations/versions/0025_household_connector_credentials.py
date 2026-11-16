"""Store encrypted household connector credentials with expiry tracking."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_household_connector_credentials"
down_revision = "0024_merge_cat_translations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "household_connector_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("auth_mode", sa.Text(), nullable=False),
        sa.Column("encrypted_credentials", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.UniqueConstraint("household_id", "provider_id", name="uq_household_connector_provider"),
    )
    op.create_index(
        "ix_household_connector_credentials_household_id",
        "household_connector_credentials",
        ["household_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_household_connector_credentials_household_id",
        table_name="household_connector_credentials",
    )
    op.drop_table("household_connector_credentials")
