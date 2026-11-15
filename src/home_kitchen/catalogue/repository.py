"""PostgreSQL reader for the migration-seeded generic food catalogue."""

from sqlalchemy import text
from sqlalchemy.engine import Engine

from home_kitchen.catalogue.models import FoodCatalogueItem


class PostgresFoodCatalogueRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def list_all(self) -> list[FoodCatalogueItem]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, source, source_item_id, source_name, display_name, locale, "
                    "food_group_id, category, "
                    "default_unit, search_keywords, source_version, source_url, source_attribution "
                    "FROM kitchen_food_catalogue ORDER BY lower(display_name), id"
                )
            ).mappings()
            return [FoodCatalogueItem.model_validate(dict(row)) for row in rows]
