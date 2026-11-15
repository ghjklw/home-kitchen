"""Private, household-scoped source photographs retained before transcription."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015_recipe_photos"
down_revision = "0014_recipe_source_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recipe_photos",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("household_id", sa.Uuid, nullable=False, index=True),
        sa.Column("data", sa.LargeBinary, nullable=False),
        sa.Column("mime_type", sa.Text, nullable=False),
        sa.Column("width", sa.Integer, nullable=False),
        sa.Column("height", sa.Integer, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("book_title", sa.Text),
        sa.Column("page", sa.Text),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("octet_length(data) BETWEEN 1 AND 8388608"),
        sa.CheckConstraint("width > 0 AND height > 0 AND width::bigint * height <= 20000000"),
        sa.CheckConstraint("mime_type IN ('image/jpeg', 'image/png', 'image/webp')"),
    )
    op.add_column("recipes", sa.Column("photo_source", JSONB))
    op.add_column("recipes", sa.Column("photo_source_id", sa.Uuid))
    op.create_foreign_key(
        "fk_recipes_photo_source", "recipes", "recipe_photos", ["photo_source_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_recipes_photo_source", "recipes", type_="foreignkey")
    op.drop_column("recipes", "photo_source_id")
    op.drop_column("recipes", "photo_source")
    op.drop_table("recipe_photos")
