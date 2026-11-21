"""Bootstrap and dependency injection wiring for recipe collections, discovery, and images."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from home_kitchen.catalogue.adapters import CatalogueRecipeAutoMapperAdapter
from home_kitchen.catalogue.identity_repository import PostgresIdentityRepository
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.imports import import_recipe
from home_kitchen.models import Recipe
from home_kitchen.recipes.adapters.discovery import (
    MarmitonDiscoveryProvider,
    MatpratDiscoveryProvider,
)
from home_kitchen.recipes.adapters.memory import (
    MemoryCollectionUnitOfWork,
    MemoryRecipeImageRepository,
    MemoryRecipeRepository,
)
from home_kitchen.recipes.adapters.oda import OdaDiscoveryProvider
from home_kitchen.recipes.adapters.postgres import (
    CataloguePostImportMapper,
    PostgresCollectionUnitOfWork,
    PostgresHouseholdLocaleAdapter,
    PostgresRecipeImageRepository,
    PostgresRecipeRepository,
)
from home_kitchen.recipes.adapters.wikimedia import WikimediaCommonsImageProvider
from home_kitchen.recipes.collections import CollectionProvider, CollectionService
from home_kitchen.recipes.discovery import DiscoveryContext, RecipeDiscoveryRegistry
from home_kitchen.recipes.images import ImageSearchProvider, RecipeImageService
from home_kitchen.recipes.ports import (
    HistoryRecordPort,
    HouseholdLocalePort,
    OdaRecipeSearchClient,
    PostImportIngredientMapper,
    RecipeAutoMapperPort,
    RecipeExtractor,
    RecipeIdentityPort,
    RecipeIdentityResolver,
    RecipeMealReferenceChecker,
    RecipeRepository,
    RecipeStatsReader,
    RecipeTranslationRepository,
)
from home_kitchen.recipes.service import RecipeService
from home_kitchen.recipes.translation import AIRecipeTranslator, RecipeTranslator

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


def create_recipe_service(
    engine: Any,
    history: HistoryRecordPort | None = None,
    *,
    identity_service: CanonicalIdentityService | None = None,
    translator: RecipeTranslator | None = None,
    settings: Any = None,
    household_id: UUID | None = None,
    photo_household_id_provider: Callable[[], UUID | None] | None = None,
    recipes: RecipeRepository | None = None,
    translations: RecipeTranslationRepository | None = None,
    stats: RecipeStatsReader | None = None,
    meal_checker: RecipeMealReferenceChecker | None = None,
    locale_reader: HouseholdLocalePort | None = None,
    auto_mapper: RecipeAutoMapperPort | None = None,
) -> RecipeService:
    """Bootstrap a production `RecipeService` backed by PostgreSQL/SQLAlchemy."""
    from home_kitchen.history import HistoryRepository
    from home_kitchen.store import resolve_recipe_mapping_repository

    resolved_history: HistoryRecordPort = history or HistoryRepository(engine)
    pg_repo = PostgresRecipeRepository(
        engine,
        resolved_history,
        photo_household_id=household_id,
        photo_household_id_provider=photo_household_id_provider,
    )
    resolved_identities: Any = identity_service or CanonicalIdentityService(
        PostgresIdentityRepository(engine)
    )
    resolved_auto_mapper = auto_mapper or CatalogueRecipeAutoMapperAdapter(
        resolve_recipe_mapping_repository(
            pg_repo if hasattr(pg_repo, "engine") else type("_E", (), {"engine": engine})()
        ),
        settings_provider=lambda: settings,
        household_id_provider=lambda: household_id,
        identity_service=resolved_identities,
    )

    def _default_translator() -> RecipeTranslator | None:
        if settings is not None and settings.ai_configured:
            return AIRecipeTranslator(settings)
        return None

    return RecipeService(
        recipes=recipes or pg_repo,
        identities=resolved_identities,
        translations=translations or pg_repo,
        stats=stats or pg_repo,
        meal_checker=meal_checker or pg_repo,
        locale_reader=locale_reader or PostgresHouseholdLocaleAdapter(engine),
        auto_mapper=resolved_auto_mapper,
        translator=translator,
        default_translator_factory=_default_translator,
        household_id=household_id,
    )


def create_in_memory_recipe_service(
    repository: MemoryRecipeRepository | None = None,
    *,
    identities: RecipeIdentityPort,
    initial_recipes: list[Recipe] | None = None,
    household_locale: str = "nb",
    planned_recipe_ids: set[UUID] | None = None,
    translator: RecipeTranslator | None = None,
    auto_mapper: RecipeAutoMapperPort | None = None,
    household_id: UUID | None = None,
) -> tuple[RecipeService, MemoryRecipeRepository]:
    """Bootstrap an in-memory `RecipeService` for unit tests without a database."""
    repo = (
        repository
        if repository is not None
        else MemoryRecipeRepository(
            initial_recipes=initial_recipes,
            household_locale=household_locale,
            planned_recipe_ids=planned_recipe_ids,
        )
    )
    service = RecipeService(
        recipes=repo,
        identities=identities,
        translations=repo,
        stats=repo,
        meal_checker=repo,
        locale_reader=repo,
        auto_mapper=auto_mapper,
        translator=translator,
        household_id=household_id,
    )
    return service, repo


def create_collection_uow_for_kitchen(
    kitchen: Any,
    history: HistoryRecordPort | None = None,
    *,
    identity_resolver: RecipeIdentityResolver | None = None,
) -> tuple[PostgresCollectionUnitOfWork, RecipeIdentityResolver | None]:
    """Build a PostgreSQL collection UoW and identity resolver from a legacy KitchenService."""
    from home_kitchen.history import HistoryRepository

    engine = kitchen.store.engine
    resolved_history: HistoryRecordPort = history or HistoryRepository(engine)
    uow = PostgresCollectionUnitOfWork(engine, resolved_history)
    resolved_identity: RecipeIdentityResolver | None = identity_resolver
    if resolved_identity is None and callable(getattr(kitchen, "resolve_recipe_identities", None)):
        resolved_identity = kitchen
    return uow, resolved_identity


def create_cookidoo_recipe_service(
    kitchen: Any,
    history: HistoryRecordPort | None,
    store: Any,
    household_id: UUID,
    *,
    collection_provider: Any | None = None,
    identity_resolver: RecipeIdentityResolver | None = None,
    transport: Any | None = None,
    base_url: str | None = None,
) -> Any:
    """Bootstrap a production `CookidooRecipeService` backed by PostgreSQL/SQLAlchemy."""
    from home_kitchen.cookidoo_recipes import CookidooRecipeService

    uow, resolved_identity = create_collection_uow_for_kitchen(
        kitchen, history, identity_resolver=identity_resolver
    )
    return CookidooRecipeService(
        uow,
        history,
        store,
        household_id,
        collection_provider=collection_provider,
        identity_resolver=resolved_identity,
        transport=transport,
        base_url=base_url,
    )


def create_in_memory_cookidoo_recipe_service(
    uow: MemoryCollectionUnitOfWork | None = None,
    *,
    store: Any | None = None,
    household_id: UUID | None = None,
    collection_provider: Any | None = None,
    identity_resolver: RecipeIdentityResolver | None = None,
    transport: Any | None = None,
    base_url: str | None = None,
) -> tuple[Any, MemoryCollectionUnitOfWork, Any]:
    """Bootstrap an in-memory `CookidooRecipeService` for unit tests without a database."""
    from uuid import uuid4

    from home_kitchen.connector_store import MemoryConnectorStore
    from home_kitchen.cookidoo_recipes import CookidooRecipeService

    resolved_uow = uow if uow is not None else MemoryCollectionUnitOfWork()
    resolved_store = store if store is not None else MemoryConnectorStore()
    resolved_hid = household_id if household_id is not None else uuid4()
    service = CookidooRecipeService(
        resolved_uow,
        None,
        resolved_store,
        resolved_hid,
        collection_provider=collection_provider,
        identity_resolver=identity_resolver,
        transport=transport,
        base_url=base_url,
    )
    return service, resolved_uow, resolved_store
