"""Track reviewed shopping-source closures separately from purchases."""

import sqlalchemy as sa
from alembic import op

revision = "0020_trip_source_closures"
down_revision = "0019_photo_draft_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_trip_source_closures",
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("need_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column(
            "resolution",
            sa.String(length=24),
            nullable=False,
        ),
        sa.Column(
            "trip_id",
            sa.Uuid(),
            sa.ForeignKey("kitchen_completed_trips.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("week_start", "week_end", "need_id", "source_kind", "source_id"),
        sa.CheckConstraint("week_end >= week_start", name="ck_trip_source_week_order"),
        sa.CheckConstraint(
            "source_kind IN ('meal', 'capture', 'regular')",
            name="ck_trip_source_kind",
        ),
        sa.CheckConstraint(
            "resolution IN ('purchased', 'already_have', 'not_needed', 'unavailable', 'other')",
            name="ck_trip_source_resolution",
        ),
    )
    op.create_index(
        "ix_kitchen_trip_source_closures_trip",
        "kitchen_trip_source_closures",
        ["trip_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kitchen_trip_source_closures_trip",
        table_name="kitchen_trip_source_closures",
    )
    op.drop_table("kitchen_trip_source_closures")
