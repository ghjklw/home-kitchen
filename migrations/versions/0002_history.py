"""Add durable household history with PostgreSQL full-text retrieval.

Revision ID: 0002_history
Revises: 0001_core
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_history"
down_revision = "0001_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_history",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column(
            "structured", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        # Deliberately no recipe FK: history remains after recipe deletion.
        sa.Column("recipe_id", sa.Uuid, nullable=True),
        sa.Column("session_id", sa.Text, nullable=True),
        sa.Column("actor_id", sa.Text, nullable=True),
        sa.Column(
            "search_document",
            postgresql.TSVECTOR,
            sa.Computed(
                "to_tsvector('simple', message || ' ' || structured::text)", persisted=True
            ),
            nullable=False,
        ),
        sa.CheckConstraint("channel IN ('app', 'api', 'mcp')", name="ck_kitchen_history_channel"),
        sa.CheckConstraint(
            "kind IN ('chat', 'feedback', 'preference', 'purchase', 'recipe_rating', "
            "'planning', 'domain_event')",
            name="ck_kitchen_history_kind",
        ),
    )
    op.create_index(
        "ix_kitchen_history_search_document",
        "kitchen_history",
        ["search_document"],
        postgresql_using="gin",
    )
    op.create_index("ix_kitchen_history_occurred_at", "kitchen_history", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_kitchen_history_occurred_at", table_name="kitchen_history")
    op.drop_index("ix_kitchen_history_search_document", table_name="kitchen_history")
    op.drop_table("kitchen_history")
