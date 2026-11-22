"""Add optimistic-concurrency versions to planned meals."""

import sqlalchemy as sa
from alembic import op

revision = "0033_meal_versions"
down_revision = "0032_kassal_ean_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meals",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("meals", "version")
