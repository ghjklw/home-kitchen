"""PostgreSQL / SQLAlchemy secondary adapters for recipe collections and images."""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    ForeignKey,
    Table,
    Text,
    Uuid,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from home_kitchen.context import history_context
from home_kitchen.history import HistoryEntryCreate, HistoryRepository
from home_kitchen.models import (
    Recipe,
    RecipeActivityStats,
    RecipeImageAttribution,
    RecipeTranslation,
)
from home_kitchen.recipes.ports import (
    CollectionSession,
    GenerationLockableProvider,
    HistoryRecordPort,
)
from home_kitchen.settings import Household
from home_kitchen.store import (
    entity_values,
    meals,
    metadata,
    recipe_translations,
    recipes,
    resolve_recipe_mapping_repository,
)
from home_kitchen.store import (
    settings as settings_table,
)

source_links = Table(
    "recipe_source_links",
    metadata,
    Column("provider", Text, primary_key=True),
    Column("external_recipe_id", Text, primary_key=True),
    Column("recipe_id", Uuid, ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False),
    Column("source_url", Text, nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def _find_existing_recipe_id(
    connection: Connection,
    provider: str,
    external_recipe_id: str,
    source_url: str | None,
) -> UUID | None:
    linked = connection.execute(
        select(source_links.c.recipe_id).where(
            source_links.c.provider == provider,
            source_links.c.external_recipe_id == external_recipe_id,
        )
    ).scalar_one_or_none()
    if linked is not None or source_url is None:
        return linked
    return connection.execute(
        select(recipes.c.id)
        .where(recipes.c.source_url == source_url)
        .order_by(recipes.c.position)
        .limit(1)
    ).scalar_one_or_none()


class PostgresCollectionSession:
    """SQLAlchemy session implementing `CollectionSession` within an active transaction."""

    def __init__(self, connection: Connection, history: HistoryRecordPort) -> None:
        self._connection = connection
        self._history = history

    def acquire_source_lock(self, provider: str, external_recipe_id: str) -> None:
        if self._connection.dialect.name == "postgresql":
            self._connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"recipe-source:{provider}:{external_recipe_id}"},
            )

    def lock_provider_generation(
        self, provider: GenerationLockableProvider, actor_id: str, generation: object
    ) -> None:
        provider.lock_generation(self._connection, actor_id, generation)

    def find_existing_recipe_id(
        self, provider: str, external_recipe_id: str, source_url: str | None
    ) -> UUID | None:
        return _find_existing_recipe_id(self._connection, provider, external_recipe_id, source_url)

    def save_recipe(self, recipe: Recipe) -> None:
        self._connection.execute(insert(recipes).values(**entity_values(recipe)))

    def record_import_history(
        self,
        *,
        actor_id: str,
        recipe_id: UUID,
        provider: str,
        external_recipe_id: str,
        source_url: str | None,
        badges: list[str],
        message: str,
    ) -> None:
        self._history.record(
            HistoryEntryCreate(
                kind="domain_event",
                message=message,
                recipe_id=recipe_id,
                structured={
                    "provider": provider,
                    "external_recipe_id": external_recipe_id,
                    "source_url": source_url,
                    "badges": badges,
                },
            ),
            actor_id=actor_id,
            connection=self._connection,
        )

    def link_source(
        self,
        *,
        provider: str,
        external_recipe_id: str,
        recipe_id: UUID,
        source_url: str | None,
        metadata: dict[str, Any],
    ) -> None:
        link_insert = (
            pg_insert(source_links)
            if self._connection.dialect.name == "postgresql"
            else sqlite_insert(source_links)
        )
        self._connection.execute(
            link_insert.values(
                provider=provider,
                external_recipe_id=external_recipe_id,
                recipe_id=recipe_id,
                source_url=source_url,
                metadata=metadata,
                created_at=datetime.now(UTC),
            ).on_conflict_do_nothing(
                index_elements=[source_links.c.provider, source_links.c.external_recipe_id]
            )
        )


class PostgresCollectionUnitOfWork:
    """SQLAlchemy unit of work for collection duplicate checks and atomic imports."""

    def __init__(self, engine: Engine, history: HistoryRecordPort) -> None:
        self._engine = engine
        self.history = history

    def find_existing_recipe_id(
        self, provider: str, external_recipe_id: str, source_url: str | None
    ) -> UUID | None:
        with self._engine.connect() as connection:
            return _find_existing_recipe_id(connection, provider, external_recipe_id, source_url)

    @contextmanager
    def session(self) -> Generator[CollectionSession]:
        with self._engine.begin() as connection:
            yield PostgresCollectionSession(connection, self.history)


class CataloguePostImportMapper:
    """Adapter bridging collection imports to catalogue AI ingredient mapping."""

    def __init__(
        self,
        store: Any,
        settings: Any,
        *,
        household_id: UUID | None = None,
        identity_service: Any = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._household_id = household_id
        self._identity_service = identity_service

    async def map_imported_recipes(self, actor_id: str, recipe_ids: list[UUID]) -> None:
        if self._settings is None or not self._settings.ai_configured:
            return
        from home_kitchen.catalogue.ai_mapper import auto_map_unmapped_ingredients_async

        await auto_map_unmapped_ingredients_async(
            resolve_recipe_mapping_repository(self._store),
            self._settings,
            household_id=self._household_id,
            actor_id=actor_id,
            recipe_ids=recipe_ids,
            batch_size=40,
            max_batches=10,
            identity_service=self._identity_service,
        )


class PostgresRecipeImageRepository:
    """SQLAlchemy / PostgreSQL adapter for persisting reviewed recipe image selections."""

    def __init__(self, engine: Engine, history: HistoryRecordPort) -> None:
        self._engine = engine
        self._history = history

    def save_selected_image(
        self,
        *,
        actor_id: str,
        recipe_id: UUID,
        image_url: str,
        attribution: RecipeImageAttribution,
        provider: str,
        provider_id: str,
        source_page_url: str,
        license_name: str,
        license_url: str,
    ) -> Recipe:
        target_id = UUID(str(recipe_id))
        attribution_json = attribution.model_dump(mode="json")
        with self._engine.begin() as connection:
            updated = connection.execute(
                update(recipes)
                .where(recipes.c.id == target_id)
                .values(image_url=str(image_url), image_attribution=attribution_json)
                .returning(recipes.c.id)
            ).scalar_one_or_none()
            if updated is None:
                raise KeyError(str(recipe_id))
            self._history.record(
                HistoryEntryCreate(
                    kind="domain_event",
                    message="Selected a recipe image",
                    recipe_id=recipe_id,
                    structured={
                        "provider": provider,
                        "provider_id": provider_id,
                        "source_page_url": source_page_url,
                        "license_name": license_name,
                        "license_url": license_url,
                    },
                ),
                actor_id=actor_id,
                connection=connection,
            )
            row = (
                connection.execute(select(recipes).where(recipes.c.id == target_id))
                .mappings()
                .one()
            )
        return Recipe.model_validate(dict(row))


class PostgresRecipeRepository:
    """SQLAlchemy / PostgreSQL adapter for recipes, translations, meal refs, and stats."""

    def __init__(
        self,
        engine: Engine | None,
        history: HistoryRecordPort | None = None,
        *,
        photo_household_id: UUID | None = None,
        photo_household_id_provider: Callable[[], UUID | None] | None = None,
        connection_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._engine = engine
        self._history: HistoryRecordPort | None = (
            history
            if history is not None
            else (HistoryRepository(engine) if engine is not None else None)
        )
        self._photo_household_id = photo_household_id
        self._photo_household_id_provider = photo_household_id_provider
        self._connection_provider = connection_provider

    @contextmanager
    def _connection(self) -> Generator[Any]:
        if self._connection_provider is not None:
            with self._connection_provider() as connection:
                yield connection
            return
        if self._engine is None:
            raise RuntimeError("PostgresRecipeRepository requires an engine or connection_provider")
        with self._engine.begin() as connection:
            yield connection

    def _record_history(self, entry: HistoryEntryCreate, *, connection: Any) -> None:
        if self._history is not None:
            self._history.record(entry, connection=connection)

    def save(self, recipe: Recipe) -> None:
        values = entity_values(recipe)
        statement = pg_insert(recipes).values(**values)
        conflict = [recipes.c.id]
        photo_household_id = (
            self._photo_household_id_provider()
            if self._photo_household_id_provider is not None
            else self._photo_household_id
        )
        with self._connection() as connection:
            if recipe.photo_source is not None:
                from home_kitchen.recipes.photo_storage import recipe_photos

                # Serialize reference creation with discard/expiry by locking
                # the photo row in this transaction before saving the recipe.
                query = select(recipe_photos.c.id).where(
                    recipe_photos.c.id == recipe.photo_source.id
                )
                if photo_household_id is not None:
                    query = query.where(recipe_photos.c.household_id == photo_household_id)
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
            connection.execute(
                statement.on_conflict_do_update(index_elements=conflict, set_=values)
            )
            self._record_history(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="domain_event",
                    message="Saved recipe",
                    structured={
                        "entity_type": "recipe",
                        "entity_id": str(recipe.id),
                        "data": recipe.model_dump(mode="json"),
                    },
                    recipe_id=recipe.id,
                ),
                connection=connection,
            )

    def get(self, recipe_id: UUID) -> Recipe:
        with self._connection() as connection:
            row = (
                connection.execute(select(recipes).where(recipes.c.id == recipe_id))
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(str(recipe_id))
        return Recipe.model_validate(dict(row))

    def list_all(self) -> list[Recipe]:
        with self._connection() as connection:
            rows = connection.execute(select(recipes).order_by(recipes.c.position)).mappings().all()
        return [Recipe.model_validate(dict(row)) for row in rows]

    def delete(self, recipe_id: UUID) -> None:
        with self._connection() as connection:
            result = connection.execute(recipes.delete().where(recipes.c.id == recipe_id))
            if result.rowcount == 0:
                raise KeyError(str(recipe_id))

    def has_meals_for_recipe(self, recipe_id: UUID) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                select(meals.c.id).where(meals.c.recipe_id == recipe_id).limit(1)
            ).first()
        return row is not None

    def save_translation(self, translation: RecipeTranslation) -> None:
        values = translation.model_dump()
        values["ingredients"] = [ing.model_dump(mode="json") for ing in translation.ingredients]
        values.pop("is_stale", None)
        statement = pg_insert(recipe_translations).values(**values)
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
        with self._connection() as connection:
            connection.execute(
                statement.on_conflict_do_update(index_elements=conflict, set_=update_set)
            )
            self._record_history(
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

    def get_translation(
        self, recipe_id: UUID, target_locale: str, recipe_version: str | None = None
    ) -> RecipeTranslation | None:
        query = select(recipe_translations).where(
            recipe_translations.c.recipe_id == recipe_id,
            recipe_translations.c.target_locale == target_locale,
        )
        if recipe_version is not None:
            query = query.where(recipe_translations.c.recipe_version == recipe_version)
        query = query.order_by(recipe_translations.c.created_at.desc())
        with self._connection() as connection:
            row = connection.execute(query).mappings().first()
        if row is None:
            return None
        return RecipeTranslation.model_validate(dict(row))

    def list_translations(self, recipe_id: UUID) -> list[RecipeTranslation]:
        query = (
            select(recipe_translations)
            .where(recipe_translations.c.recipe_id == recipe_id)
            .order_by(recipe_translations.c.created_at.desc())
        )
        with self._connection() as connection:
            rows = connection.execute(query).mappings().all()
        return [RecipeTranslation.model_validate(dict(row)) for row in rows]

    def get_recipe_activity_stats(self, recipe_id: UUID) -> RecipeActivityStats:
        stats = self.get_recipes_activity_stats([recipe_id])
        return stats.get(recipe_id, RecipeActivityStats())

    def get_recipes_activity_stats(
        self, recipe_ids: list[UUID] | None = None
    ) -> dict[UUID, RecipeActivityStats]:
        target_id_filter = set(recipe_ids) if recipe_ids is not None else None
        unique_ids = list(dict.fromkeys(recipe_ids)) if recipe_ids is not None else None
        with self._connection() as connection:
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

                is_cooking = kind != "planning" and (
                    kind in ("cooked", "cooking")
                    or action in ("recipe_cooked", "cooked", "meal_cooked", "cooking_completed")
                    or event in ("recipe_cooked", "cooked", "meal_cooked", "cooking_completed")
                    or (kind == "domain_event" and ("cook" in action or "cook" in event))
                )
                if is_cooking:
                    cooked_counts[r_id] = cooked_counts.get(r_id, 0) + 1

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


class PostgresHouseholdLocaleAdapter:
    """SQLAlchemy / PostgreSQL adapter reading household default language from settings."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_household_locale(self) -> str:
        try:
            with self._engine.begin() as connection:
                value = connection.execute(
                    select(settings_table.c.value).where(settings_table.c.key == "household")
                ).scalar()
            if value:
                return Household.model_validate_json(value).language
        except Exception:
            pass
        return "nb"
