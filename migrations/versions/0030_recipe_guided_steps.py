"""Add guided_steps JSONB column to recipes for Thermomix TM6 step metadata.

Revision ID: 0030_recipe_guided_steps
Revises: 0029_plan_proposal_edits
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030_recipe_guided_steps"
down_revision = "0029_plan_proposal_edits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recipes",
        sa.Column(
            "guided_steps",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("recipes", "guided_steps")
