"""Store encrypted, actor-scoped Oda OAuth connections and one-time PKCE state."""

import sqlalchemy as sa
from alembic import op

revision = "0006_oda"
down_revision = "0005_store_trip"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_oda_lifecycles",
        sa.Column("actor_id", sa.Text, primary_key=True),
        sa.Column("generation", sa.Integer, nullable=False),
    )
    op.create_table(
        "kitchen_oda_connections",
        sa.Column("actor_id", sa.Text, primary_key=True),
        sa.Column("encrypted_payload", sa.Text, nullable=False),
    )
    op.create_table(
        "kitchen_oda_states",
        sa.Column("state_hash", sa.Text, primary_key=True),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("encrypted_payload", sa.Text, nullable=False),
    )
    op.create_index("ix_kitchen_oda_states_actor_id", "kitchen_oda_states", ["actor_id"])


def downgrade() -> None:
    op.drop_table("kitchen_oda_states")
    op.drop_table("kitchen_oda_connections")
    op.drop_table("kitchen_oda_lifecycles")
