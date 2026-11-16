"""Allow providers without a documented update timestamp."""

from alembic import op
from sqlalchemy import text

revision = "0021_nullable_source_time"
down_revision = "0020_trip_source_closures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("provider_order_sources", "source_updated_at", nullable=True)


def downgrade() -> None:
    has_unknown_source_time = (
        op.get_bind()
        .execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM provider_order_sources "
                "WHERE source_updated_at IS NULL)"
            )
        )
        .scalar_one()
    )
    if has_unknown_source_time:
        raise RuntimeError(
            "cannot downgrade while provider records have unknown source update timestamps"
        )
    op.alter_column("provider_order_sources", "source_updated_at", nullable=False)
