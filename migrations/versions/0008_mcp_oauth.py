"""Encrypted shared OAuth broker state for remote MCP clients."""

import sqlalchemy as sa
from alembic import op

revision = "0008_mcp_oauth"
down_revision = "0007_oda_cart"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_mcp_oauth",
        sa.Column("collection", sa.Text, primary_key=True),
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("encrypted_payload", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_kitchen_mcp_oauth_expires_at", "kitchen_mcp_oauth", ["expires_at"])


def downgrade() -> None:
    op.drop_table("kitchen_mcp_oauth")
