"""Support explicit target-meal edits alongside additions in meal plan proposals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0029_plan_proposal_edits"
down_revision = "0028_identity_correction_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kitchen_plan_proposals",
        sa.Column(
            "meal_fingerprints",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "kitchen_plan_proposals",
        sa.Column(
            "target_meals",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("kitchen_plan_proposals", "target_meals")
    op.drop_column("kitchen_plan_proposals", "meal_fingerprints")
