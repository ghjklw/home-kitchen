"""Transactional PostgreSQL storage for kitchen records."""

from __future__ import annotations

import builtins
import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
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

    def save(self, kind: str, entity: Recipe | Meal | RegularItem | ShoppingItem) -> None:
        table = TABLES[kind]
        values = entity_values(entity)
        statement = insert(table).values(**values)
        conflict = [table.c.id]
        with self.connection() as connection:
            if isinstance(entity, Recipe) and entity.photo_source is not None:
                from .recipes.photo_storage import recipe_photos

                # Serialize reference creation with discard/expiry by locking
                # the photo row in this transaction before saving the recipe.
                query = select(recipe_photos.c.id).where(
                    recipe_photos.c.id == entity.photo_source.id
                )
                if self.photo_household_id is not None:
                    query = query.where(recipe_photos.c.household_id == self.photo_household_id)
                photo_id = connection.execute(query.with_for_update()).scalar_one_or_none()
                if photo_id is None:
                    raise ValueError("Photo source was not found")
                connection.execute(
                    recipe_photos.update()
                    .where(
                        recipe_photos.c.id == photo_id,
                        recipe_photos.c.retained_at.is_(None),
                    )
                    .values(retained_at=datetime.now(UTC))
                )
            previous = None
            if isinstance(entity, RegularItem):
                previous = connection.execute(
                    select(regulars.c.last_purchased).where(regulars.c.id == entity.id)
                ).first()
            if kind == "meal":
                inserted = connection.execute(
                    statement.on_conflict_do_nothing(index_elements=conflict).returning(table.c.id)
                ).scalar_one_or_none()
                if inserted is None:
                    raise MealIDConflict("A meal with this ID already exists")
            else:
                connection.execute(
                    statement.on_conflict_do_update(index_elements=conflict, set_=values)
                )
            purchased = (
                isinstance(entity, RegularItem)
                and entity.last_purchased is not None
                and (previous is None or previous[0] != entity.last_purchased)
            )
            HistoryRepository(self.engine).record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="purchase"
                    if purchased
                    else "planning"
                    if kind == "meal"
                    else "domain_event",
                    message=f"Purchased {entity.name}" if purchased else f"Saved {kind}",
                    structured={
                        "entity_type": kind,
                        "entity_id": str(entity.id),
                        "data": entity.model_dump(mode="json"),
                    },
                    recipe_id=entity.id
                    if isinstance(entity, Recipe)
                    else entity.recipe_id
                    if isinstance(entity, Meal)
                    else None,
                ),
                connection=connection,
            )

    def update_meal(self, entity: Meal) -> None:
        values = entity_values(entity)
        values.pop("id")
        with self.connection() as connection:
            result = connection.execute(
                update(meals).where(meals.c.id == entity.id).values(**values)
            )
            if result.rowcount == 0:
                raise KeyError(str(entity.id))
            HistoryRepository(self.engine).record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="planning",
                    message="Updated planned meal",
                    recipe_id=entity.recipe_id,
                    structured={
                        "entity_type": "meal",
                        "entity_id": str(entity.id),
                        "data": entity.model_dump(mode="json"),
                    },
                ),
                connection=connection,
            )

    def get(self, kind: str, entity_id: UUID, model: type[Model]) -> Model:
        table = TABLES[kind]
        with self.connection() as connection:
            row = (
                connection.execute(select(table).where(table.c.id == entity_id)).mappings().first()
            )
        if row is None:
            raise KeyError(str(entity_id))
        return model.model_validate(dict(row))

    def list(self, kind: str, model: type[Model]) -> builtins.list[Model]:
        with self.connection() as connection:
            rows = (
                connection.execute(select(TABLES[kind]).order_by(TABLES[kind].c.position))
                .mappings()
                .all()
            )
        return [model.model_validate(dict(row)) for row in rows]

    def delete(self, kind: str, entity_id: UUID) -> None:
        table = TABLES[kind]
        with self.connection() as connection:
            result = connection.execute(table.delete().where(table.c.id == entity_id))
            if result.rowcount == 0:
                raise KeyError(str(entity_id))

    def active_confirmations(self) -> set[UUID]:
        with self.connection() as connection:
            return set(
                connection.execute(
                    select(confirmations.c.regular_id)
                    .join(items)
                    .where(items.c.completed.is_(False))
                ).scalars()
            )

    def confirm(self, regular_id: UUID, item: ShoppingItem) -> ShoppingItem:
        """Lock the regular record so concurrent requests reuse its active capture."""
        with self.connection() as connection:
            regular = connection.execute(
                select(regulars.c.id).where(regulars.c.id == regular_id).with_for_update()
            ).first()
            if regular is None:
                raise KeyError(str(regular_id))
            row = (
                connection.execute(
                    select(items)
                    .join(confirmations)
                    .where(confirmations.c.regular_id == regular_id)
                )
                .mappings()
                .first()
            )
            if row is not None and not row["completed"]:
                return ShoppingItem.model_validate(dict(row))
            connection.execute(insert(items).values(**entity_values(item)))
            statement = insert(confirmations).values(regular_id=regular_id, item_id=item.id)
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[confirmations.c.regular_id], set_={"item_id": item.id}
                )
            )
        return item

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
        values = translation.model_dump()
        values["ingredients"] = [ing.model_dump(mode="json") for ing in translation.ingredients]
        values.pop("is_stale", None)
        statement = insert(recipe_translations).values(**values)
        conflict = [
            recipe_translations.c.recipe_id,
            recipe_translations.c.target_locale,
            recipe_translations.c.recipe_version,
        ]
        update_set = {
            "title": values["title"],
            "instructions": values["instructions"],
            "ingredients": values["ingredients"],
            "provider": values["provider"],
            "model": values["model"],
            "created_at": values["created_at"],
        }
        with self.connection() as connection:
            connection.execute(
                statement.on_conflict_do_update(index_elements=conflict, set_=update_set)
            )
            HistoryRepository(self.engine).record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="domain_event",
                    message=f"Saved {translation.target_locale} translation for recipe",
                    recipe_id=translation.recipe_id,
                    structured={
                        "entity_type": "recipe_translation",
                        "entity_id": str(translation.id),
                        "target_locale": translation.target_locale,
                        "recipe_version": translation.recipe_version,
                        "provider": translation.provider,
                        "model": translation.model,
                    },
                ),
                connection=connection,
            )

    def get_recipe_translation(
        self, recipe_id: UUID, target_locale: str, recipe_version: str | None = None
    ) -> RecipeTranslation | None:
        query = select(recipe_translations).where(
            recipe_translations.c.recipe_id == recipe_id,
            recipe_translations.c.target_locale == target_locale,
        )
        if recipe_version is not None:
            query = query.where(recipe_translations.c.recipe_version == recipe_version)
        query = query.order_by(recipe_translations.c.created_at.desc())
        with self.connection() as connection:
            row = connection.execute(query).mappings().first()
        if row is None:
            return None
        return RecipeTranslation.model_validate(dict(row))

    def list_recipe_translations(self, recipe_id: UUID) -> builtins.list[RecipeTranslation]:
        query = (
            select(recipe_translations)
            .where(recipe_translations.c.recipe_id == recipe_id)
            .order_by(recipe_translations.c.created_at.desc())
        )
        with self.connection() as connection:
            rows = connection.execute(query).mappings().all()
        return [RecipeTranslation.model_validate(dict(row)) for row in rows]

    def get_recipe_activity_stats(self, recipe_id: UUID) -> RecipeActivityStats:
        stats = self.get_recipes_activity_stats([recipe_id])
        return stats.get(recipe_id, RecipeActivityStats())

    def get_recipes_activity_stats(
        self, recipe_ids: builtins.list[UUID] | None = None
    ) -> dict[UUID, RecipeActivityStats]:
        target_id_filter = set(recipe_ids) if recipe_ids is not None else None
        unique_ids = list(dict.fromkeys(recipe_ids)) if recipe_ids is not None else None
        with self.connection() as connection:
            planned_counts: dict[UUID, int] = {}
            if unique_ids is not None:
                if unique_ids:
                    meal_rows = (
                        connection.execute(
                            text(
                                "SELECT recipe_id, count(*) AS count "
                                "FROM meals "
                                "WHERE recipe_id = ANY(:ids) "
                                "GROUP BY recipe_id"
                            ),
                            {"ids": unique_ids},
                        )
                        .mappings()
                        .all()
                    )
                else:
                    meal_rows = []
            else:
                meal_rows = (
                    connection.execute(
                        text("SELECT recipe_id, count(*) AS count FROM meals GROUP BY recipe_id")
                    )
                    .mappings()
                    .all()
                )

            for row in meal_rows:
                r_id = row["recipe_id"]
                if isinstance(r_id, str):
                    r_id = UUID(r_id)
                planned_counts[r_id] = int(row["count"])

            if unique_ids is not None:
                if unique_ids:
                    id_strs = [str(i) for i in unique_ids]
                    history_rows = (
                        connection.execute(
                            text(
                                "SELECT id, occurred_at, kind, recipe_id, structured "
                                "FROM kitchen_history "
                                "WHERE recipe_id = ANY(:ids) OR structured->>'recipe_id' = ANY(:id_strs) "
                                "ORDER BY occurred_at DESC, id DESC"
                            ),
                            {"ids": unique_ids, "id_strs": id_strs},
                        )
                        .mappings()
                        .all()
                    )
                else:
                    history_rows = []
            else:
                history_rows = (
                    connection.execute(
                        text(
                            "SELECT id, occurred_at, kind, recipe_id, structured "
                            "FROM kitchen_history "
                            "WHERE recipe_id IS NOT NULL OR structured->>'recipe_id' IS NOT NULL "
                            "ORDER BY occurred_at DESC, id DESC"
                        )
                    )
                    .mappings()
                    .all()
                )

            cooked_counts: dict[UUID, int] = {}
            ordered_counts: dict[UUID, int] = {}
            feedback_counts: dict[UUID, int] = {}
            ratings_by_recipe: dict[UUID, list[int]] = {}
            latest_rating_by_recipe: dict[UUID, int] = {}
            would_make_by_recipe: dict[UUID, bool] = {}

            for row in history_rows:
                r_id = row["recipe_id"]
                structured = row["structured"] or {}
                if isinstance(structured, str):
                    try:
                        structured = json.loads(structured)
                    except Exception:
                        structured = {}
                if r_id is None and "recipe_id" in structured:
                    try:
                        r_id = UUID(str(structured["recipe_id"]))
                    except (ValueError, TypeError):
                        r_id = None
                if r_id is None:
                    continue
                if isinstance(r_id, str):
                    r_id = UUID(r_id)
                if target_id_filter is not None and r_id not in target_id_filter:
                    continue

                kind = str(row["kind"])
                action = str(structured.get("action") or "").lower()
                event = str(structured.get("event") or "").lower()

                # Confirmed cooking check (never infer from planning or unconfirmed meals)
                is_cooking = kind != "planning" and (
                    kind in ("cooked", "cooking")
                    or action in ("recipe_cooked", "cooked", "meal_cooked", "cooking_completed")
                    or event in ("recipe_cooked", "cooked", "meal_cooked", "cooking_completed")
                    or (kind == "domain_event" and ("cook" in action or "cook" in event))
                )
                if is_cooking:
                    cooked_counts[r_id] = cooked_counts.get(r_id, 0) + 1

                # Order/purchase check traced to this recipe
                is_ordered = (
                    kind == "purchase"
                    or action
                    in (
                        "recipe_ordered",
                        "order_placed",
                        "provider_order",
                        "order_matched",
                        "oda_order_matched",
                    )
                    or event in ("order", "order_placed", "recipe_ordered", "oda_order")
                    or (kind == "domain_event" and ("order" in action or "order" in event))
                )
                if is_ordered:
                    ordered_counts[r_id] = ordered_counts.get(r_id, 0) + 1

                # Feedback check
                if kind == "recipe_rating":
                    feedback_counts[r_id] = feedback_counts.get(r_id, 0) + 1
                    rating = structured.get("rating")
                    if (
                        isinstance(rating, int)
                        and not isinstance(rating, bool)
                        and 1 <= rating <= 5
                    ):
                        ratings_by_recipe.setdefault(r_id, []).append(rating)
                        if r_id not in latest_rating_by_recipe:
                            latest_rating_by_recipe[r_id] = rating
                    would_make = structured.get("would_make_again")
                    if isinstance(would_make, bool):
                        if r_id not in would_make_by_recipe:
                            would_make_by_recipe[r_id] = would_make

            if target_id_filter is not None:
                target_ids = target_id_filter
            else:
                target_ids = {r[0] for r in connection.execute(select(recipes.c.id)).all()}
                target_ids.update(planned_counts.keys())
                target_ids.update(cooked_counts.keys())
                target_ids.update(ordered_counts.keys())
                target_ids.update(feedback_counts.keys())

            result: dict[UUID, RecipeActivityStats] = {}
            for target_id in target_ids:
                ratings = ratings_by_recipe.get(target_id, [])
                avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
                result[target_id] = RecipeActivityStats(
                    times_planned=planned_counts.get(target_id, 0),
                    times_cooked=cooked_counts.get(target_id, 0),
                    times_ordered=ordered_counts.get(target_id, 0),
                    latest_rating=latest_rating_by_recipe.get(target_id),
                    average_rating=avg_rating,
                    would_make_again=would_make_by_recipe.get(target_id),
                    feedback_count=feedback_counts.get(target_id, 0),
                )
            return result

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
