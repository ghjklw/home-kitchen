"""PostgreSQL / SQLAlchemy secondary adapters for recipe collections and images."""

from __future__ import annotations

from collections.abc import Generator
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

from home_kitchen.history import HistoryEntryCreate
from home_kitchen.models import Recipe, RecipeImageAttribution
from home_kitchen.recipes.ports import (
    CollectionSession,
    GenerationLockableProvider,
    HistoryRecordPort,
)
from home_kitchen.store import (
    entity_values,
    metadata,
    recipes,
    resolve_recipe_mapping_repository,
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
