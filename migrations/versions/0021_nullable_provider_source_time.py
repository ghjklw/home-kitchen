"""Allow providers without an authoritative source update timestamp."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "0021_provider_source_time"
down_revision = "0020_trip_source_closures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "provider_order_sources",
        "source_updated_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )


def downgrade() -> None:
    # Do not invent source timestamps to make existing captures fit the old schema.
    has_unknown_source_time = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM provider_order_sources WHERE source_updated_at IS NULL)"
            )
        )
        .scalar_one()
    )
    if has_unknown_source_time:
        raise CommandError(
            "Cannot downgrade 0021_provider_source_time: provider_order_sources contains NULL "
            "source_updated_at values. Preserve these source captures and resolve the schema "
            "compatibility before downgrading; source timestamps must not be fabricated."
        )
    op.alter_column(
        "provider_order_sources",
        "source_updated_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
