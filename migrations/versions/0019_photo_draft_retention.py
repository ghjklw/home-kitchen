"""Expire abandoned private photos, preserving existing saved sources."""

import sqlalchemy as sa
from alembic import op

revision = "0019_photo_draft_retention"
down_revision = "0018_provider_order_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recipe_photos", sa.Column("last_draft_activity_at", sa.DateTime(timezone=True)))
    op.add_column("recipe_photos", sa.Column("retained_at", sa.DateTime(timezone=True)))
    # Existing drafts get a full grace period after deployment. Photos already
    # attached to a recipe are permanently protected, even if later detached.
    op.execute("UPDATE recipe_photos SET last_draft_activity_at = CURRENT_TIMESTAMP")
    op.execute(
        "UPDATE recipe_photos SET retained_at = CURRENT_TIMESTAMP "
        "WHERE EXISTS (SELECT 1 FROM recipes WHERE recipes.photo_source_id = recipe_photos.id)"
    )
    op.alter_column("recipe_photos", "last_draft_activity_at", nullable=False)
    op.create_index(
        "ix_recipe_photos_last_draft_activity_at", "recipe_photos", ["last_draft_activity_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_recipe_photos_last_draft_activity_at", table_name="recipe_photos")
    op.drop_column("recipe_photos", "retained_at")
    op.drop_column("recipe_photos", "last_draft_activity_at")
