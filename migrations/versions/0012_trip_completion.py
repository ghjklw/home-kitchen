"""Append-only explicit completed purchase snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_trip_completion"
down_revision = "0011_catalogue_skus"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_completed_trips",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("idempotency_key", sa.Uuid, nullable=False, unique=True),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.Text),
    )
    op.execute("""
        CREATE FUNCTION kitchen_completed_trip_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Completed trips are append-only'; END; $$
    """)
    op.execute("""
        CREATE TRIGGER kitchen_completed_trip_immutable BEFORE UPDATE OR DELETE
        ON kitchen_completed_trips FOR EACH ROW EXECUTE FUNCTION kitchen_completed_trip_immutable()
    """)


def downgrade() -> None:
    op.drop_table("kitchen_completed_trips")
    op.execute("DROP FUNCTION kitchen_completed_trip_immutable()")
