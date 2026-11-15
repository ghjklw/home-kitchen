"""Reviewable actor-bound atomic meal plan proposals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013_plan_proposals"
down_revision = "0012_trip_completion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_plan_proposals",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column("meals", JSONB, nullable=False),
        sa.Column("recipe_fingerprints", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("applied_meal_ids", JSONB, nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.CheckConstraint("status IN ('pending', 'applied', 'expired', 'stale')"),
    )


def downgrade() -> None:
    op.drop_table("kitchen_plan_proposals")
