"""Transactional PostgreSQL storage for kitchen records."""

from __future__ import annotations

import builtins
import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    create_engine,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Connection, Engine, make_url

if TYPE_CHECKING:
    from home_kitchen.catalogue.identity_service import IdentityRepository
    from home_kitchen.planning.adapters.postgres import PostgresMealAgendaRepository
    from home_kitchen.recipes.adapters.postgres import PostgresRecipeRepository
    from home_kitchen.shopping_adapters import PostgresShoppingItemRepository

from .context import history_context
from .history import HistoryEntryCreate, HistoryRepository
from .models import Meal, Recipe, RecipeActivityStats, RecipeTranslation, RegularItem, ShoppingItem

Model = TypeVar("Model", bound=BaseModel)
metadata = MetaData()
recipes = Table(
    "recipes",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("position", BigInteger, Identity(), nullable=False, unique=True),
    Column("title", Text, nullable=False),
    Column("servings", Integer, nullable=False),
    Column("ingredients", JSON().with_variant(JSONB, "postgresql"), nullable=False),
    Column("instructions", JSON().with_variant(JSONB, "postgresql"), nullable=False),
    Column("source_url", Text),
    Column("image_url", Text),
    Column("source", Text, nullable=False),
    Column("source_ingredients", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("provenance", JSONB),
    Column("photo_source", JSONB),
    Column("photo_source_id", Uuid, ForeignKey("recipe_photos.id")),
    Column("image_attribution", JSONB),
    Column("source_language", Text, nullable=False, server_default="und"),
    Column("recipe_type", Text),
    Column("prep_time_minutes", Integer),
    Column("prep_category", Text),
    Column(
        "guided_steps",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        server_default=text("'[]'::jsonb"),
    ),
)
recipe_translations = Table(
    "kitchen_recipe_translations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("recipe_id", Uuid, ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False),
    Column("target_locale", Text, nullable=False),
    Column("recipe_version", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("instructions", JSON().with_variant(JSONB, "postgresql"), nullable=False),
    Column("ingredients", JSON().with_variant(JSONB, "postgresql"), nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint(
        "recipe_id", "target_locale", "recipe_version", name="uq_recipe_translation_version"
    ),
)
meals = Table(
    "meals",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("position", BigInteger, Identity(), nullable=False, unique=True),
    Column("date", Date, nullable=False),
    Column("label", Text),
    Column("scheduled_time", Time),
    Column("recipe_id", Uuid, ForeignKey("recipes.id"), nullable=False),
    Column("servings", Integer, nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
)
items = Table(
    "items",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("position", BigInteger, Identity(), nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("category", Text, nullable=False, server_default="Other"),
    Column("quantity", Numeric, nullable=False),
    Column("unit", Text, nullable=False),
    Column("completed", Boolean, nullable=False),
)
regulars = Table(
    "regulars",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("position", BigInteger, Identity(), nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("category", Text, nullable=False, server_default="Other"),
    Column("quantity", Numeric, nullable=False),
    Column("unit", Text, nullable=False),
    Column("interval_days", Integer, nullable=False),
    Column("last_purchased", Date),
)
confirmations = Table(
    "confirmations",
    metadata,
    Column("regular_id", Uuid, ForeignKey("regulars.id", ondelete="CASCADE"), primary_key=True),
    Column("item_id", Uuid, ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
)
settings = Table(
    "settings",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)


class MealIDConflict(ValueError):
    """A meal create must never replace an existing meal ID."""


# Kept as an import-compatible name for callers catching the prior conflict.
MealSlotOccupied = MealIDConflict


TABLES = {"recipe": recipes, "meal": meals, "item": items, "regular": regulars}


def entity_values(entity: BaseModel) -> dict:
    values = entity.model_dump()
    # JSON columns contain JSON values; native dates, UUIDs and decimals elsewhere
    # retain their database types and precision.
    if isinstance(entity, Recipe):
        values.pop("activity_stats", None)
        values.pop("stats", None)
        json_values = entity.model_dump(mode="json")
        for field in (
            "ingredients",
            "source_ingredients",
            "provenance",
            "photo_source",
            "image_attribution",
            "guided_steps",
        ):
            values[field] = json_values[field]
        values["photo_source_id"] = entity.photo_source.id if entity.photo_source else None
        values["source_url"] = str(entity.source_url) if entity.source_url else None
        values["image_url"] = str(entity.image_url) if entity.image_url else None
        values["recipe_type"] = str(entity.recipe_type) if entity.recipe_type else None
        values["prep_category"] = str(entity.prep_category) if entity.prep_category else None
    return values


def get_expected_alembic_head() -> tuple[str | None, int]:
    """Return (single_head_revision, head_count) from the packaged Alembic script directory."""
    package_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    ini_path = package_ini if package_ini.is_file() else Path.cwd() / "alembic.ini"
    config = AlembicConfig(str(ini_path))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    return (heads[0] if len(heads) == 1 else None), len(heads)


class Store:
    def __init__(self, database_url: str):
        if make_url(database_url).drivername != "postgresql+psycopg":
            raise ValueError("database_url must use postgresql+psycopg")
        self.engine = create_engine(database_url, pool_pre_ping=True)
        # Set by KitchenFeatures after the server household is resolved. Saves
        # then validate photo ownership against the same scope as photo reads.
        self.photo_household_id: UUID | None = None

    @contextmanager
    def connection(self) -> Generator[Connection]:
        with self.engine.begin() as connection:
            yield connection

    def health(self) -> None:
        with self.connection() as connection:
            connection.execute(text("SELECT 1"))

    def migration_status(self, expected_head: str | None) -> tuple[bool, tuple[str, ...]]:
        """Check database reachability and whether Alembic schema matches expected_head."""
        with self.connection() as connection:
            connection.execute(text("SELECT 1"))
            current_heads = tuple(
                sorted(MigrationContext.configure(connection).get_current_heads())
            )
        is_current = expected_head is not None and current_heads == (expected_head,)
        return is_current, current_heads

    def _recipes_repo(self) -> PostgresRecipeRepository:
        from home_kitchen.recipes.adapters.postgres import PostgresRecipeRepository

        return PostgresRecipeRepository(
            self.engine,
            photo_household_id_provider=lambda: self.photo_household_id,
            connection_provider=self.connection,
        )

    def _meals_repo(self) -> PostgresMealAgendaRepository:
        from home_kitchen.planning.adapters.postgres import PostgresMealAgendaRepository

        return PostgresMealAgendaRepository(self.engine)

    def _shopping_repo(self) -> PostgresShoppingItemRepository:
        from home_kitchen.shopping_adapters import PostgresShoppingItemRepository

        return PostgresShoppingItemRepository(self.engine)

    def save(self, kind: str, entity: Recipe | Meal | RegularItem | ShoppingItem) -> None:
        if isinstance(entity, Recipe):
            self._recipes_repo().save(entity)
        elif isinstance(entity, Meal):
            self._meals_repo().save_meal(entity)
        elif isinstance(entity, ShoppingItem):
            self._shopping_repo().save_item(entity)
        elif isinstance(entity, RegularItem):
            self._shopping_repo().save_regular(entity)
        else:
            raise KeyError(kind)

    def update_meal(self, entity: Meal, *, expected_version: int) -> Meal:
        return self._meals_repo().update_meal(entity, expected_version=expected_version)

    def get(self, kind: str, entity_id: UUID, model: type[Model]) -> Model:
        if kind == "recipe":
            result: BaseModel = self._recipes_repo().get(entity_id)
        elif kind == "meal":
            result = self._meals_repo().get_meal(entity_id)
        elif kind == "item":
            result = self._shopping_repo().get_item(entity_id)
        elif kind == "regular":
            result = self._shopping_repo().get_regular(entity_id)
        else:
            raise KeyError(kind)
        if isinstance(result, model):
            return result
        return model.model_validate(result.model_dump())

    def list(self, kind: str, model: type[Model]) -> builtins.list[Model]:
        if kind == "recipe":
            rows: Sequence[BaseModel] = self._recipes_repo().list_all()
        elif kind == "meal":
            rows = self._meals_repo().list_meals()
        elif kind == "item":
            rows = self._shopping_repo().list_items()
        elif kind == "regular":
            rows = self._shopping_repo().list_regulars()
        else:
            raise KeyError(kind)
        return [
            item if isinstance(item, model) else model.model_validate(item.model_dump())
            for item in rows
        ]

    def delete(self, kind: str, entity_id: UUID) -> None:
        if kind == "recipe":
            self._recipes_repo().delete(entity_id)
        elif kind == "meal":
            self._meals_repo().delete_meal(entity_id)
        elif kind == "item":
            self._shopping_repo().delete_item(entity_id)
        elif kind == "regular":
            with self.connection() as connection:
                result = connection.execute(regulars.delete().where(regulars.c.id == entity_id))
                if result.rowcount == 0:
                    raise KeyError(str(entity_id))
        else:
            raise KeyError(kind)

    def active_confirmations(self) -> set[UUID]:
        return self._shopping_repo().active_confirmations()

    def confirm(self, regular_id: UUID, item: ShoppingItem) -> ShoppingItem:
        """Lock the regular record so concurrent requests reuse its active capture."""
        return self._shopping_repo().confirm_regular(regular_id, item)

    def get_setting(self, key: str) -> str | None:
        with self.connection() as connection:
            return connection.execute(
                select(settings.c.value).where(settings.c.key == key)
            ).scalar()

    def ensure_setting(self, key: str, value: str) -> str:
        """Initialize a deployment setting once, including concurrent startups."""
        with self.connection() as connection:
            connection.execute(
                insert(settings)
                .values(key=key, value=value)
                .on_conflict_do_nothing(index_elements=[settings.c.key])
            )
            return connection.execute(
                select(settings.c.value).where(settings.c.key == key)
            ).scalar_one()

    def set_setting(self, key: str, value: str) -> None:
        statement = insert(settings).values(key=key, value=value)
        with self.connection() as connection:
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[settings.c.key], set_={"value": value}
                )
            )

    def save_recipe_translation(self, translation: RecipeTranslation) -> None:
        self._recipes_repo().save_translation(translation)

    def get_recipe_translation(
        self, recipe_id: UUID, target_locale: str, recipe_version: str | None = None
    ) -> RecipeTranslation | None:
        return self._recipes_repo().get_translation(recipe_id, target_locale, recipe_version)

    def list_recipe_translations(self, recipe_id: UUID) -> builtins.list[RecipeTranslation]:
        return self._recipes_repo().list_translations(recipe_id)

    def get_recipe_activity_stats(self, recipe_id: UUID) -> RecipeActivityStats:
        return self._recipes_repo().get_recipe_activity_stats(recipe_id)

    def get_recipes_activity_stats(
        self, recipe_ids: builtins.list[UUID] | None = None
    ) -> dict[UUID, RecipeActivityStats]:
        return self._recipes_repo().get_recipes_activity_stats(recipe_ids)

    def get_recipe(self, recipe_id: UUID) -> Recipe:
        return self.get("recipe", recipe_id, Recipe)

    def get_recipes(self, recipe_ids: Sequence[UUID]) -> builtins.list[Recipe]:
        return EngineRecipeMappingRepository(self.engine).get_recipes(recipe_ids)

    def list_recipes(self) -> builtins.list[Recipe]:
        return self.list("recipe", Recipe)

    def save_mapped_recipes(
        self,
        modified_recipes: Sequence[Recipe],
        *,
        mapped_occurrences: int,
        household_id: UUID | None,
        actor_id: str,
    ) -> None:
        EngineRecipeMappingRepository(self.engine).save_mapped_recipes(
            modified_recipes,
            mapped_occurrences=mapped_occurrences,
            household_id=household_id,
            actor_id=actor_id,
        )

    def identity_repository(self) -> IdentityRepository:
        from home_kitchen.catalogue.identity_repository import PostgresIdentityRepository

        return PostgresIdentityRepository(self.engine)


def _row_to_recipe(row: dict[str, Any]) -> Recipe:
    decoded = dict(row)
    for json_field in (
        "ingredients",
        "instructions",
        "source_ingredients",
        "provenance",
        "photo_source",
        "image_attribution",
    ):
        value = decoded.get(json_field)
        if isinstance(value, str):
            decoded[json_field] = json.loads(value)
    return Recipe.model_validate(decoded)


class EngineRecipeMappingRepository:
    """SQLAlchemy recipe-mapping repository backed by an Engine."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def get_recipe(self, recipe_id: UUID) -> Recipe:
        with self.engine.connect() as conn:
            row = conn.execute(select(recipes).where(recipes.c.id == recipe_id)).mappings().first()
        if row is None:
            raise KeyError(str(recipe_id))
        return _row_to_recipe(dict(row))

    def get_recipes(self, recipe_ids: Sequence[UUID]) -> builtins.list[Recipe]:
        id_list = list(dict.fromkeys(recipe_ids))
        if not id_list:
            return []
        with self.engine.connect() as conn:
            rows = conn.execute(select(recipes).where(recipes.c.id.in_(id_list))).mappings().all()
        by_id = {recipe.id: recipe for recipe in (_row_to_recipe(dict(r)) for r in rows)}
        return [by_id[rid] for rid in id_list if rid in by_id]

    def list_recipes(self) -> builtins.list[Recipe]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(recipes).order_by(recipes.c.position)).mappings().all()
        return [_row_to_recipe(dict(r)) for r in rows]

    def save_mapped_recipes(
        self,
        modified_recipes: Sequence[Recipe],
        *,
        mapped_occurrences: int,
        household_id: UUID | None,
        actor_id: str,
    ) -> None:
        if not modified_recipes:
            return
        with self.engine.begin() as conn:
            for updated_recipe in modified_recipes:
                serialized_ingredients = [
                    ing.model_dump(mode="json") for ing in updated_recipe.ingredients
                ]
                conn.execute(
                    update(recipes)
                    .where(recipes.c.id == updated_recipe.id)
                    .values(ingredients=serialized_ingredients)
                )
            if conn.dialect.name == "postgresql":
                HistoryRepository(self.engine).record(
                    HistoryEntryCreate(
                        channel=history_context.get().channel,
                        kind="domain_event",
                        message=f"Auto-mapped {mapped_occurrences} ingredient occurrences",
                        structured={
                            "entity_type": "ingredient_auto_map",
                            "mapped_occurrences": mapped_occurrences,
                            "household_id": str(household_id) if household_id else None,
                        },
                        recipe_id=modified_recipes[0].id if len(modified_recipes) == 1 else None,
                    ),
                    actor_id=actor_id,
                    connection=conn,
                )

    def identity_repository(self) -> IdentityRepository:
        from home_kitchen.catalogue.identity_repository import PostgresIdentityRepository

        return PostgresIdentityRepository(self.engine)


def resolve_recipe_mapping_repository(store: Any) -> Any:
    """Return a RecipeMappingRepository-compatible object for a Store, engine wrapper, or mock."""
    if (
        callable(getattr(store, "get_recipe", None))
        and callable(getattr(store, "get_recipes", None))
        and callable(getattr(store, "list_recipes", None))
        and callable(getattr(store, "save_mapped_recipes", None))
    ):
        return store
    engine = getattr(store, "engine", None)
    if engine is not None and not hasattr(store, "recipes"):
        return EngineRecipeMappingRepository(engine)
    return store
