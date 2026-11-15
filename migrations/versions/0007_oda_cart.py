"""Durable encrypted one-use Oda cart reviews and non-retryable outcomes."""

import sqlalchemy as sa
from alembic import op

revision = "0007_oda_cart"
down_revision = "0006_oda"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_oda_cart_reviews",
        sa.Column("id_hash", sa.Text, primary_key=True),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("encrypted_payload", sa.Text, nullable=False),
    )
    op.create_index(
        "ix_kitchen_oda_cart_reviews_actor_id", "kitchen_oda_cart_reviews", ["actor_id"]
    )


def downgrade() -> None:
    op.drop_table("kitchen_oda_cart_reviews")
