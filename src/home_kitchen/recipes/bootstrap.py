"""Bootstrap and dependency injection wiring for recipe collections, discovery, and images."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from home_kitchen.imports import import_recipe
from home_kitchen.models import Recipe
from home_kitchen.recipes.adapters.discovery import (
    MarmitonDiscoveryProvider,
    MatpratDiscoveryProvider,
)
from home_kitchen.recipes.adapters.memory import (
    MemoryCollectionUnitOfWork,
    MemoryRecipeImageRepository,
)
from home_kitchen.recipes.adapters.oda import OdaDiscoveryProvider
from home_kitchen.recipes.adapters.postgres import (
    CataloguePostImportMapper,
    PostgresCollectionUnitOfWork,
    PostgresRecipeImageRepository,
)
from home_kitchen.recipes.adapters.wikimedia import WikimediaCommonsImageProvider
from home_kitchen.recipes.collections import CollectionProvider, CollectionService
from home_kitchen.recipes.discovery import DiscoveryContext, RecipeDiscoveryRegistry
from home_kitchen.recipes.images import ImageSearchProvider, RecipeImageService
from home_kitchen.recipes.ports import (
    HistoryRecordPort,
    OdaRecipeSearchClient,
    PostImportIngredientMapper,
    RecipeExtractor,
    RecipeIdentityResolver,
)

if TYPE_CHECKING:
    from home_kitchen.service import KitchenService


def create_collection_service(
    kitchen: KitchenService,
    history: HistoryRecordPort | None = None,
    *,
    provider: CollectionProvider | None = None,
    providers: dict[str, CollectionProvider] | None = None,
    extractor: RecipeExtractor | None = None,
) -> CollectionService:
    """Bootstrap a production `CollectionService` backed by PostgreSQL/SQLAlchemy."""
    from home_kitchen.history import HistoryRepository

    resolved_history: HistoryRecordPort = history or HistoryRepository(kitchen.store.engine)
    uow = PostgresCollectionUnitOfWork(kitchen.store.engine, resolved_history)
    identity_resolver: RecipeIdentityResolver | None = (
        kitchen if callable(getattr(kitchen, "resolve_recipe_identities", None)) else None
    )
    post_import_mapper = CataloguePostImportMapper(
        kitchen.store,
        getattr(kitchen, "settings", None),
        household_id=getattr(kitchen, "household_id", None),
        identity_service=getattr(kitchen, "identities", None),
    )
    return CollectionService(
        uow,
        provider=provider,
        extractor=extractor,
        providers=providers,
        identity_resolver=identity_resolver,
        post_import_mapper=post_import_mapper,
        default_extractor=import_recipe,
    )


def create_in_memory_collection_service(
    uow: MemoryCollectionUnitOfWork | None = None,
    *,
    provider: CollectionProvider | None = None,
    providers: dict[str, CollectionProvider] | None = None,
    extractor: RecipeExtractor | None = None,
    identity_resolver: RecipeIdentityResolver | None = None,
    post_import_mapper: PostImportIngredientMapper | None = None,
    default_extractor: RecipeExtractor | None = None,
) -> tuple[CollectionService, MemoryCollectionUnitOfWork]:
    """Bootstrap an in-memory `CollectionService` for unit tests without a database."""
    resolved_uow = uow if uow is not None else MemoryCollectionUnitOfWork()
    service = CollectionService(
        resolved_uow,
        provider=provider,
        extractor=extractor,
        providers=providers,
        identity_resolver=identity_resolver,
        post_import_mapper=post_import_mapper,
        default_extractor=default_extractor,
    )
    return service, resolved_uow


def create_recipe_image_service(
    kitchen: KitchenService,
    history: HistoryRecordPort | None = None,
    *,
    provider: ImageSearchProvider | None = None,
) -> RecipeImageService:
    """Bootstrap a production `RecipeImageService` backed by PostgreSQL/SQLAlchemy."""
    from home_kitchen.history import HistoryRepository

    resolved_history: HistoryRecordPort = history or HistoryRepository(kitchen.store.engine)
    repository = PostgresRecipeImageRepository(kitchen.store.engine, resolved_history)
    return RecipeImageService(
        repository,
        provider=provider or WikimediaCommonsImageProvider(),
    )


def create_in_memory_recipe_image_service(
    repository: MemoryRecipeImageRepository | None = None,
    *,
    initial_recipes: list[Recipe] | None = None,
    provider: ImageSearchProvider | None = None,
) -> tuple[RecipeImageService, MemoryRecipeImageRepository]:
    """Bootstrap an in-memory `RecipeImageService` for unit tests without a database."""
    resolved_repo = (
        repository
        if repository is not None
        else MemoryRecipeImageRepository(initial_recipes=initial_recipes)
    )
    return RecipeImageService(resolved_repo, provider=provider), resolved_repo


def create_recipe_discovery_registry(
    oda: OdaRecipeSearchClient,
    *,
    context_provider: Callable[[str], DiscoveryContext] | None = None,
    extra_providers: tuple[Any, ...] = (),
) -> RecipeDiscoveryRegistry:
    """Bootstrap a `RecipeDiscoveryRegistry` with Oda, Marmiton, and Matprat providers."""
    return RecipeDiscoveryRegistry(
        [
            OdaDiscoveryProvider(oda),
            MarmitonDiscoveryProvider(),
            MatpratDiscoveryProvider(),
            *extra_providers,
        ],
        context_provider=context_provider,
    )
