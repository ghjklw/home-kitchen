"""Add household_id and source_type to canonical product and unit alias tables."""

import sqlalchemy as sa
from alembic import op

revision = "0031_alias_household_source_type"
down_revision = "0030_plan_proposal_edits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, fk, global_uq, household_uq, household_lookup_idx in (
        (
            "kitchen_product_aliases",
            "product_id",
            "uq_kitchen_product_aliases_global",
            "uq_kitchen_product_aliases_household",
            "ix_product_alias_household_lookup",
        ),
        (
            "kitchen_unit_aliases",
            "unit_id",
            "uq_kitchen_unit_aliases_global",
            "uq_kitchen_unit_aliases_household",
            "ix_unit_alias_household_lookup",
        ),
    ):
        op.add_column(table, sa.Column("household_id", sa.Uuid(), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "source_type",
                sa.String(),
                nullable=False,
                server_default="reviewed_correction",
            ),
        )
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.create_index(
            global_uq,
            table,
            ["locale", "context", "normalized_alias", fk],
            unique=True,
            postgresql_where=sa.text("household_id IS NULL"),
        )
        op.create_index(
            household_uq,
            table,
            ["household_id", "locale", "context", "normalized_alias", fk],
            unique=True,
            postgresql_where=sa.text("household_id IS NOT NULL"),
        )
        op.create_index(
            household_lookup_idx,
            table,
            ["household_id", "locale", "context", "normalized_alias"],
        )


def downgrade() -> None:
    for table, fk, global_uq, household_uq, household_lookup_idx in (
        (
            "kitchen_unit_aliases",
            "unit_id",
            "uq_kitchen_unit_aliases_global",
            "uq_kitchen_unit_aliases_household",
            "ix_unit_alias_household_lookup",
        ),
        (
            "kitchen_product_aliases",
            "product_id",
            "uq_kitchen_product_aliases_global",
            "uq_kitchen_product_aliases_household",
            "ix_product_alias_household_lookup",
        ),
    ):
        op.drop_index(household_lookup_idx, table_name=table)
        op.drop_index(household_uq, table_name=table)
        op.drop_index(global_uq, table_name=table)
        op.execute(f"DELETE FROM {table} WHERE household_id IS NOT NULL")
        op.create_primary_key(
            f"{table}_pkey",
            table,
            ["locale", "context", "normalized_alias", fk],
        )
        op.drop_column(table, "source_type")
        op.drop_column(table, "household_id")
