"""Persist selected recipe image attribution independently from recipe source."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016_recipe_image_attribution"
down_revision = "0015_recipe_photos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recipes", sa.Column("image_attribution", JSONB))


def downgrade() -> None:
    op.drop_column("recipes", "image_attribution")
