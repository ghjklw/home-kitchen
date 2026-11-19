"""Add auditable product and unit mapping correction events."""

import sqlalchemy as sa
from alembic import op

revision = "0028_identity_correction_audit"
down_revision = "0027_multi_account_connectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kitchen_identity_correction_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("normalized_alias", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("previous_identity_id", sa.Uuid(), nullable=True),
        sa.Column("new_identity_id", sa.Uuid(), nullable=False),
        sa.Column("previous_reviewed", sa.Boolean(), nullable=True),
        sa.Column("new_reviewed", sa.Boolean(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_kitchen_identity_correction_events_household_occurred",
        "kitchen_identity_correction_events",
        ["household_id", "occurred_at"],
    )
    op.create_index(
        "ix_kitchen_identity_correction_events_lookup",
        "kitchen_identity_correction_events",
        ["kind", "locale", "context", "normalized_alias"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kitchen_identity_correction_events_lookup",
        table_name="kitchen_identity_correction_events",
    )
    op.drop_index(
        "ix_kitchen_identity_correction_events_household_occurred",
        table_name="kitchen_identity_correction_events",
    )
    op.drop_table("kitchen_identity_correction_events")
