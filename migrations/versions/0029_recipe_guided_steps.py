"""Add guided_steps JSONB column to recipes for Thermomix TM6 step metadata.

Revision ID: 0029_recipe_guided_steps
Revises: 0028_identity_correction_audit
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029_recipe_guided_steps"
down_revision = "0028_identity_correction_audit"
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
