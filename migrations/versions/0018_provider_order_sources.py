"""Store provider order source revisions and per-account page checkpoints."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_provider_order_sources"
down_revision = "0017_flexible_meals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_order_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("account_id", sa.String(255), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=False),
        sa.Column("revision_id", sa.String(512), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint(
            "provider",
            "account_id",
            "external_id",
            "revision_id",
            name="uq_provider_order_revision",
        ),
    )
    op.create_table(
        "provider_order_checkpoints",
        sa.Column("provider", sa.String(100), primary_key=True),
        sa.Column("account_id", sa.String(255), primary_key=True),
        sa.Column("cursor", sa.String(2048), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("provider_order_checkpoints")
    op.drop_table("provider_order_sources")
