"""Keep in-store picking progress separate from shopping needs and purchases."""

import sqlalchemy as sa
from alembic import op

revision = "0005_store_trip"
down_revision = "0004_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_store_trip",
        sa.Column("week_start", sa.Date, primary_key=True),
        sa.Column("week_end", sa.Date, primary_key=True),
        sa.Column("need_id", sa.Uuid, primary_key=True),
        sa.Column("checked_quantity", sa.Numeric, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("week_end >= week_start", name="ck_store_trip_week"),
        sa.CheckConstraint("checked_quantity >= 0", name="ck_store_trip_quantity"),
    )


def downgrade() -> None:
    op.drop_table("kitchen_store_trip")
