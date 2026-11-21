"""Narrow Protocol ports for recipe collections, discovery, and image selection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from home_kitchen.models import (
    GuidedCookingStep,
    PrepCategory,
    Recipe,
    RecipeImageAttribution,
    RecipeType,
)

if TYPE_CHECKING:
    from home_kitchen.history import HistoryEntryCreate


class ExtractedRecipeDraft(Protocol):
    """Structural contract for an extracted recipe draft before saving."""

    title: str
    servings: int
    ingredients: list[str]
    instructions: list[str]
    source_url: Any
    image_url: Any
    recipe_type: RecipeType | None
    prep_time_minutes: int | None
    prep_category: PrepCategory | None
    guided_steps: list[GuidedCookingStep]


RecipeExtractor = Callable[[str], Awaitable[ExtractedRecipeDraft]]


class GenerationLockableProvider(Protocol):
    """Collection provider capability for locking its source connection generation."""

    def lock_generation(self, connection: Any, actor_id: str, generation: object) -> None: ...


class CollectionSession(Protocol):
    """Transactional session for collection duplicate checks and atomic recipe import."""

    def acquire_source_lock(self, provider: str, external_recipe_id: str) -> None: ...

    def lock_provider_generation(
        self, provider: GenerationLockableProvider, actor_id: str, generation: object
    ) -> None: ...

    def find_existing_recipe_id(
        self, provider: str, external_recipe_id: str, source_url: str | None
    ) -> UUID | None: ...

    def save_recipe(self, recipe: Recipe) -> None: ...

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
    ) -> None: ...

    def link_source(
        self,
        *,
        provider: str,
        external_recipe_id: str,
        recipe_id: UUID,
        source_url: str | None,
        metadata: dict[str, Any],
    ) -> None: ...


class CollectionUnitOfWork(Protocol):
    """Port for read-only collection lookups and atomic import transactions."""

    def find_existing_recipe_id(
        self, provider: str, external_recipe_id: str, source_url: str | None
    ) -> UUID | None: ...

    def session(self) -> AbstractContextManager[CollectionSession]: ...


class RecipeIdentityResolver(Protocol):
    """Port for fast canonical product and unit alias resolution on a recipe."""

    def resolve_recipe_identities(self, recipe: Recipe) -> Recipe: ...


class PostImportIngredientMapper(Protocol):
    """Port for optional bulk ingredient mapping after collection import."""

    async def map_imported_recipes(self, actor_id: str, recipe_ids: list[UUID]) -> None: ...


class HistoryRecordPort(Protocol):
    """Narrow port for recording domain events in an active transaction."""

    def record(
        self,
        data: HistoryEntryCreate,
        actor_id: str | None = None,
        connection: Any = None,
    ) -> Any: ...


class OdaCollectionClient(Protocol):
    """Narrow port for reading Oda recipe collections and connection generation."""

    def search_cache_context(self, actor_id: str) -> int | None: ...

    def status(self, actor_id: str) -> Mapping[str, Any]: ...

    async def list_liked_recipes(
        self, actor_id: str, page: int = 1, size: int = 10
    ) -> Mapping[str, Any]: ...

    async def list_purchased_recipes(
        self, actor_id: str, page: int = 1, size: int = 10
    ) -> Mapping[str, Any]: ...


class OdaGenerationLocker(Protocol):
    """Port for locking and verifying Oda connection generation within a transaction."""

    def lock_generation(self, connection: Any, actor_id: str, generation: object) -> None: ...


class HouseholdPreferences(Protocol):
    """Narrow port supplying household dietary preferences for discovery ranking."""

    @property
    def dietary_preferences(self) -> Sequence[str]: ...


class OdaRecipeSearchClient(Protocol):
    """Narrow port for read-only Oda recipe search."""

    def status(self, actor_id: str) -> Mapping[str, Any]: ...

    async def search_recipes(
        self, actor_id: str, query: str, page: int, size: int
    ) -> Mapping[str, Any]: ...


class RecipeImageRepository(Protocol):
    """Port for persisting a reviewed image selection and returning the updated recipe."""

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
    ) -> Recipe: ...
