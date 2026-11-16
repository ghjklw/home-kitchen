"""Allow any number of labelled, optionally scheduled meals per date."""

import sqlalchemy as sa
from alembic import op

revision = "0017_flexible_meals"
down_revision = "0016_recipe_image_attribution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meals", sa.Column("label", sa.Text(), nullable=True))
    op.add_column("meals", sa.Column("scheduled_time", sa.Time(), nullable=True))
    op.execute(
        """
        UPDATE meals
        SET label = CASE lower(slot)
            WHEN 'breakfast' THEN 'Frokost'
            WHEN 'lunch' THEN 'Lunsj'
            WHEN 'dinner' THEN 'Middag'
            ELSE slot
        END
        """
    )
    op.drop_constraint("meals_date_slot_key", "meals", type_="unique")
    op.drop_column("meals", "slot")


def downgrade() -> None:
    bind = op.get_bind()
    unsupported = bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1 FROM meals
                WHERE scheduled_time IS NOT NULL
                   OR label IS NULL
                   OR label NOT IN ('Frokost', 'Lunsj', 'Middag')
            ) OR EXISTS (
                SELECT 1
                FROM meals
                GROUP BY date, CASE label
                    WHEN 'Frokost' THEN 'breakfast'
                    WHEN 'Lunsj' THEN 'lunch'
                    ELSE 'dinner'
                END
                HAVING count(*) > 1
            )
            """
        )
    ).scalar_one()
    if unsupported:
        raise RuntimeError(
            "Cannot downgrade flexible meals while an entry has a custom label/time or maps to "
            "a duplicate legacy date/slot"
        )
    op.add_column("meals", sa.Column("slot", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE meals
        SET slot = CASE label
            WHEN 'Frokost' THEN 'breakfast'
            WHEN 'Lunsj' THEN 'lunch'
            ELSE 'dinner'
        END
        """
    )
    op.alter_column("meals", "slot", nullable=False)
    op.create_unique_constraint("meals_date_slot_key", "meals", ["date", "slot"])
    op.drop_column("meals", "scheduled_time")
    op.drop_column("meals", "label")
