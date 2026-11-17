"""Support multiple household accounts per external connector provider."""

import sqlalchemy as sa
from alembic import op

revision = "0027_multi_account_connectors"
down_revision = "0026_recipe_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "household_connector_credentials",
        sa.Column("account_id", sa.Text(), nullable=False, server_default="default"),
    )
    op.add_column(
        "household_connector_credentials",
        sa.Column("account_label", sa.Text(), nullable=True),
    )
    op.add_column(
        "household_connector_credentials",
        sa.Column("connected_by_actor_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "household_connector_credentials",
        sa.Column("updated_by_actor_id", sa.Text(), nullable=True),
    )
    op.execute(
        """
        UPDATE household_connector_credentials
        SET connected_by_actor_id = actor_id,
            updated_by_actor_id = actor_id
        WHERE actor_id IS NOT NULL
        """
    )
    op.drop_constraint(
        "uq_household_connector_provider",
        "household_connector_credentials",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_household_connector_provider_account",
        "household_connector_credentials",
        ["household_id", "provider_id", "account_id"],
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM household_connector_credentials
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY household_id, provider_id
                           ORDER BY CASE WHEN account_id = 'default' THEN 0 ELSE 1 END,
                                    updated_at DESC,
                                    id DESC
                       ) AS rn
                FROM household_connector_credentials
            ) ranked
            WHERE rn = 1
        )
        """
    )
    op.drop_constraint(
        "uq_household_connector_provider_account",
        "household_connector_credentials",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_household_connector_provider",
        "household_connector_credentials",
        ["household_id", "provider_id"],
    )
    op.drop_column("household_connector_credentials", "updated_by_actor_id")
    op.drop_column("household_connector_credentials", "connected_by_actor_id")
    op.drop_column("household_connector_credentials", "account_label")
    op.drop_column("household_connector_credentials", "account_id")
