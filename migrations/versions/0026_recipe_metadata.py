"""Add recipe metadata: recipe_type, prep_time_minutes, prep_category.

Revision ID: 0026_recipe_metadata
Revises: 0025_connector_credentials
"""

import sqlalchemy as sa
from alembic import op

revision = "0026_recipe_metadata"
down_revision = "0025_connector_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recipes", sa.Column("recipe_type", sa.Text(), nullable=True))
    op.add_column("recipes", sa.Column("prep_time_minutes", sa.Integer(), nullable=True))
    op.add_column("recipes", sa.Column("prep_category", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("recipes", "prep_category")
    op.drop_column("recipes", "prep_time_minutes")
    op.drop_column("recipes", "recipe_type")
