"""Bootstrap wiring for kitchen features using Protocol ports."""

from __future__ import annotations

from typing import Any

from home_kitchen.basket.bootstrap import create_basket_service, create_in_memory_basket_service
from home_kitchen.inventory import InventoryService
from home_kitchen.purchase_sync import ProviderAdapter
from home_kitchen.purchasing import StoreTripService
from home_kitchen.purchasing_adapters import (
    MemoryStoreTripRepository,
    MemoryTripCompletionUnitOfWork,
    PostgresStoreTripRepository,
    PostgresTripCompletionUnitOfWork,
)
from home_kitchen.purchasing_ports import (
    HistoryRecordPort,
    PurchasePlanPort,
    ShoppingListPort,
    StoreTripRepository,
    TripCompletionUnitOfWork,
)
from home_kitchen.purchasing_reconciliation import TripCompletionService
from home_kitchen.shopping import (
    ShoppingBuilder,
    ShoppingItemRepository,
    ShoppingMealReader,
    ShoppingRecipeReader,
    ShoppingService,
)
from home_kitchen.shopping_adapters import (
    MemoryShoppingItemRepository,
    PostgresShoppingItemRepository,
)

__all__ = [
    "_build_purchase_adapter",
    "create_basket_service",
    "create_connector_store",
    "create_cookidoo_plan_sync_deps",
    "create_cookidoo_plan_sync_service",
    "create_cookidoo_plan_sync_uow_for_kitchen",
    "create_in_memory_basket_service",
    "create_in_memory_cookidoo_plan_sync_service",
    "create_in_memory_purchase_sync_service",
    "create_in_memory_shopping_service",
    "create_in_memory_store_trip_service",
    "create_in_memory_trip_completion_service",
    "create_inventory_service",
    "create_kassal_client",
    "create_oda_purchase_adapter",
    "create_oda_service",
    "create_purchase_order_store",
    "create_purchase_sync_service",
    "create_shopping_service",
    "create_store_trip_service",
    "create_trip_completion_service",
    "missing_kassal_key_error",
]


def create_inventory_service(
    shopping: ShoppingListPort,
    history: HistoryRecordPort | None = None,
    *,
    engine: Any = None,
) -> InventoryService:
    """Bootstrap an `InventoryService` backed by a shopping list port and SQL engine."""
    return InventoryService(shopping, history, engine=engine)


def create_store_trip_service(
    plan_reader: PurchasePlanPort,
    history: HistoryRecordPort,
    *,
    engine: Any = None,
    repository: StoreTripRepository | None = None,
) -> StoreTripService:
    """Bootstrap a production `StoreTripService` backed by PostgreSQL."""
    resolved_engine = (
        engine
        if engine is not None
        else getattr(repository, "engine", None) or getattr(history, "engine", None)
    )
    resolved_repo = repository or PostgresStoreTripRepository(resolved_engine, history)
    return StoreTripService(plan_reader, history, repository=resolved_repo, engine=resolved_engine)


def create_in_memory_store_trip_service(
    plan_reader: PurchasePlanPort,
    history: HistoryRecordPort | None = None,
    *,
    repository: MemoryStoreTripRepository | None = None,
) -> tuple[StoreTripService, MemoryStoreTripRepository]:
    """Bootstrap an in-memory `StoreTripService` for unit tests without a database."""
    resolved_repo = repository if repository is not None else MemoryStoreTripRepository(history)
    if resolved_repo.history is None and history is not None:
        resolved_repo.history = history
    service = StoreTripService(plan_reader, history, repository=resolved_repo)
    return service, resolved_repo


def create_trip_completion_service(
    plan_reader: PurchasePlanPort,
    history: HistoryRecordPort,
    *,
    engine: Any = None,
    uow: TripCompletionUnitOfWork | None = None,
) -> TripCompletionService:
    """Bootstrap a production `TripCompletionService` backed by PostgreSQL."""
    resolved_engine = (
        engine
        if engine is not None
        else getattr(uow, "engine", None) or getattr(history, "engine", None)
    )
    resolved_uow = uow or PostgresTripCompletionUnitOfWork(resolved_engine, history)
    return TripCompletionService(plan_reader, history, uow=resolved_uow, engine=resolved_engine)


def create_in_memory_trip_completion_service(
    plan_reader: PurchasePlanPort,
    history: HistoryRecordPort | None = None,
    *,
    uow: MemoryTripCompletionUnitOfWork | None = None,
) -> tuple[TripCompletionService, MemoryTripCompletionUnitOfWork]:
    """Bootstrap an in-memory `TripCompletionService` for unit tests without a database."""
    resolved_uow = uow if uow is not None else MemoryTripCompletionUnitOfWork(history)
    if resolved_uow.history is None and history is not None:
        resolved_uow.history = history
    service = TripCompletionService(plan_reader, history, uow=resolved_uow)
    return service, resolved_uow


def create_shopping_service(
    meals: ShoppingMealReader,
    recipes: ShoppingRecipeReader,
    history: HistoryRecordPort | None = None,
    *,
    engine: Any = None,
    items: ShoppingItemRepository | None = None,
    shopping_builder: ShoppingBuilder | None = None,
) -> ShoppingService:
    """Bootstrap a production `ShoppingService` backed by PostgreSQL."""
    resolved_engine = engine if engine is not None else getattr(history, "engine", None)
    if items is not None:
        resolved_items: ShoppingItemRepository = items
    else:
        if resolved_engine is None:
            raise ValueError("create_shopping_service requires an engine or items repository")
        resolved_items = PostgresShoppingItemRepository(resolved_engine, history)
    return ShoppingService(
        resolved_items,
        meals,
        recipes,
        shopping_builder=shopping_builder,
    )


def create_in_memory_shopping_service(
    meals: ShoppingMealReader,
    recipes: ShoppingRecipeReader,
    *,
    items: MemoryShoppingItemRepository | None = None,
    shopping_builder: ShoppingBuilder | None = None,
) -> tuple[ShoppingService, MemoryShoppingItemRepository]:
    """Bootstrap an in-memory `ShoppingService` for unit tests without a database."""
    resolved_items = items if items is not None else MemoryShoppingItemRepository()
    return (
        ShoppingService(resolved_items, meals, recipes, shopping_builder=shopping_builder),
        resolved_items,
    )


def _build_purchase_adapter(
    provider: str, account_id: str, creds: dict[str, Any]
) -> ProviderAdapter | None:
    if provider == "trumf":
        cookie = creds.get("session_cookie") or creds.get("cookie")
        if not isinstance(cookie, str) or not cookie.strip():
            return None
        from home_kitchen.trumf_history import TrumfHttpSource, TrumfPurchaseHistoryAdapter

        return TrumfPurchaseHistoryAdapter(TrumfHttpSource(cookie), account_id=account_id)
    if provider == "coop":
        token = creds.get("access_token") or creds.get("session_token")
        cookie = creds.get("session_cookie") or creds.get("cookie")
        token_str = token.strip() if isinstance(token, str) and token.strip() else None
        cookie_str = cookie.strip() if isinstance(cookie, str) and cookie.strip() else None
        if not token_str and not cookie_str:
            return None
        from home_kitchen.coop_history import CoopHttpSource, CoopPurchaseHistoryAdapter

        return CoopPurchaseHistoryAdapter(
            CoopHttpSource(access_token=token_str, session_cookie=cookie_str),
            account_id=account_id,
        )
    if provider == "rema1000":
        token = creds.get("access_token") or creds.get("session_token")
        if not isinstance(token, str) or not token.strip():
            return None
        from home_kitchen.rema1000_history import (
            Rema1000HttpSource,
            Rema1000PurchaseHistoryAdapter,
        )

        return Rema1000PurchaseHistoryAdapter(Rema1000HttpSource(token), account_id=account_id)
    return None


def create_connector_store(engine: Any, settings: Any) -> Any:
    """Bootstrap a `ConnectorStore` using uniform cipher resolution."""
    from home_kitchen.connector_store import ConnectorStore
    from home_kitchen.crypto import resolve_connector_cipher

    return ConnectorStore(engine, resolve_connector_cipher(settings))


def create_oda_service(engine: Any, settings: Any, *, http_client: Any = None) -> Any:
    """Bootstrap an `OdaService` using uniform cipher resolution."""
    from home_kitchen.crypto import resolve_oda_cipher
    from home_kitchen.oda.service import OdaService

    return OdaService(
        engine,
        settings,
        http_client=http_client,
        cipher=resolve_oda_cipher(settings),
    )


def create_purchase_order_store(engine: Any) -> Any:
    """Bootstrap a PostgreSQL `PurchaseOrderStore` adapter from an engine."""
    from home_kitchen.purchase_sync_adapters import PostgresPurchaseOrderStore

    return PostgresPurchaseOrderStore(engine)


def create_kassal_client(api_key: str) -> Any:
    """Bootstrap a `KassalClient` adapter from an API key."""
    from home_kitchen.kassal import KassalClient

    return KassalClient(api_key)


def missing_kassal_key_error() -> str:
    """Return the canonical Bokmål error message when Kassal API key is missing."""
    from home_kitchen.kassal import KASSAL_MISSING_KEY_ERROR

    return KASSAL_MISSING_KEY_ERROR


def create_oda_purchase_adapter(oda_service: Any, account_id: str) -> ProviderAdapter:
    """Bootstrap an `OdaPurchaseHistoryAdapter` for one Oda actor."""
    from home_kitchen.oda.purchase_history import OdaPurchaseHistoryAdapter

    return OdaPurchaseHistoryAdapter(oda_service, account_id)


def create_purchase_sync_service(
    *,
    engine: Any = None,
    settings: Any = None,
    oda_service: Any = None,
    adapters: dict[str, ProviderAdapter] | None = None,
    order_store: Any = None,
    connector_store: Any = None,
    household_id: Any = None,
    adapter_factory: Any = None,
    connector_auth: Any = None,
    identity_service: Any = None,
    receipt_mapper: Any = None,
    kassal_client: Any = None,
) -> Any:
    """Bootstrap a production `PurchaseHistorySyncService` with injected adapters."""
    from home_kitchen.purchase_sync import PurchaseHistorySyncService

    resolved_store = order_store
    if resolved_store is None and engine is not None:
        resolved_store = create_purchase_order_store(engine)
    return PurchaseHistorySyncService(
        engine=engine,
        settings=settings,
        oda_service=oda_service,
        adapters=adapters,
        order_store=resolved_store,
        connector_store=connector_store,
        household_id=household_id,
        adapter_factory=adapter_factory or _build_purchase_adapter,
        oda_adapter_factory=create_oda_purchase_adapter,
        kassal_client_factory=create_kassal_client,
        connector_auth=connector_auth,
        identity_service=identity_service,
        receipt_mapper=receipt_mapper,
        kassal_client=kassal_client,
    )


def create_in_memory_purchase_sync_service(
    *,
    order_store: Any = None,
    connector_store: Any = None,
    household_id: Any = None,
    adapters: dict[str, ProviderAdapter] | None = None,
    settings: Any = None,
    oda_service: Any = None,
    adapter_factory: Any = None,
    oda_adapter_factory: Any = None,
    kassal_client_factory: Any = None,
    connector_auth: Any = None,
    identity_service: Any = None,
    receipt_mapper: Any = None,
    kassal_client: Any = None,
) -> tuple[Any, Any, Any]:
    """Bootstrap an in-memory `PurchaseHistorySyncService` for unit tests without a database."""
    from uuid import uuid4

    from home_kitchen.connector_store import MemoryConnectorStore
    from home_kitchen.purchase_sync import PurchaseHistorySyncService
    from home_kitchen.purchase_sync_adapters import MemoryPurchaseOrderStore

    resolved_orders = order_store if order_store is not None else MemoryPurchaseOrderStore()
    resolved_connectors = connector_store if connector_store is not None else MemoryConnectorStore()
    resolved_hid = household_id if household_id is not None else uuid4()
    service = PurchaseHistorySyncService(
        engine=None,
        settings=settings,
        oda_service=oda_service,
        adapters=adapters,
        order_store=resolved_orders,
        connector_store=resolved_connectors,
        household_id=resolved_hid,
        adapter_factory=adapter_factory,
        oda_adapter_factory=oda_adapter_factory,
        kassal_client_factory=kassal_client_factory,
        connector_auth=connector_auth,
        identity_service=identity_service,
        receipt_mapper=receipt_mapper,
        kassal_client=kassal_client,
    )
    return service, resolved_orders, resolved_connectors


def create_cookidoo_plan_sync_uow_for_kitchen(
    kitchen: Any,
    history: Any = None,
) -> Any:
    """Build a PostgreSQL Cookidoo plan sync UoW from a legacy KitchenService."""
    from home_kitchen.cookidoo_adapters import PostgresCookidooPlanSyncUnitOfWork
    from home_kitchen.history import HistoryRepository

    engine = kitchen.store.engine
    resolved_history = history or HistoryRepository(engine)
    return PostgresCookidooPlanSyncUnitOfWork(engine, resolved_history)


def create_cookidoo_plan_sync_deps(
    kitchen: Any,
    history: Any,
    store: Any,
    household_id: Any,
    *,
    provider: Any = None,
    recipe_service: Any = None,
) -> tuple[Any, Any, Any]:
    """Resolve UoW, provider, and recipe importer for `CookidooPlanSyncService`."""
    from home_kitchen.cookidoo_recipes import CookidooCollectionProvider
    from home_kitchen.recipes.bootstrap import create_cookidoo_recipe_service

    uow = create_cookidoo_plan_sync_uow_for_kitchen(kitchen, history)
    resolved_provider = provider or CookidooCollectionProvider(
        store=store, household_id=household_id
    )
    resolved_recipe_service = recipe_service or create_cookidoo_recipe_service(
        kitchen,
        history,
        store,
        household_id,
        collection_provider=resolved_provider,
    )
    return uow, resolved_provider, resolved_recipe_service


def create_cookidoo_plan_sync_service(
    kitchen: Any,
    history: Any,
    store: Any,
    household_id: Any,
    *,
    provider: Any = None,
    recipe_service: Any = None,
) -> Any:
    """Bootstrap a production `CookidooPlanSyncService` backed by PostgreSQL/SQLAlchemy."""
    from home_kitchen.cookidoo_plan_sync import CookidooPlanSyncService

    uow, resolved_provider, resolved_recipe_service = create_cookidoo_plan_sync_deps(
        kitchen,
        history,
        store,
        household_id,
        provider=provider,
        recipe_service=recipe_service,
    )
    return CookidooPlanSyncService(
        uow,
        history,
        store,
        household_id,
        provider=resolved_provider,
        recipe_service=resolved_recipe_service,
    )


def create_in_memory_cookidoo_plan_sync_service(
    *,
    uow: Any = None,
    collection_uow: Any = None,
    store: Any = None,
    household_id: Any = None,
    provider: Any = None,
    recipe_service: Any = None,
    identity_resolver: Any = None,
    transport: Any = None,
    base_url: str | None = None,
) -> tuple[Any, Any, Any, Any]:
    """Bootstrap an in-memory `CookidooPlanSyncService` for unit tests without a database."""
    from uuid import uuid4

    from home_kitchen.connector_store import MemoryConnectorStore
    from home_kitchen.cookidoo_adapters import MemoryCookidooPlanSyncUnitOfWork
    from home_kitchen.cookidoo_plan_sync import CookidooPlanSyncService
    from home_kitchen.cookidoo_recipes import CookidooCollectionProvider
    from home_kitchen.recipes.adapters.memory import MemoryCollectionUnitOfWork
    from home_kitchen.recipes.bootstrap import create_in_memory_cookidoo_recipe_service

    resolved_store = store if store is not None else MemoryConnectorStore()
    resolved_hid = household_id if household_id is not None else uuid4()
    resolved_collection_uow = (
        collection_uow if collection_uow is not None else MemoryCollectionUnitOfWork()
    )
    resolved_uow = (
        uow
        if uow is not None
        else MemoryCookidooPlanSyncUnitOfWork(
            collection_uow=resolved_collection_uow,
        )
    )
    resolved_provider = provider or CookidooCollectionProvider(
        resolved_store, resolved_hid, transport=transport, base_url=base_url
    )
    if recipe_service is None:
        resolved_recipe_service, _, _ = create_in_memory_cookidoo_recipe_service(
            resolved_collection_uow,
            store=resolved_store,
            household_id=resolved_hid,
            collection_provider=resolved_provider,
            identity_resolver=identity_resolver,
        )
    else:
        resolved_recipe_service = recipe_service
    service = CookidooPlanSyncService(
        resolved_uow,
        None,
        resolved_store,
        resolved_hid,
        provider=resolved_provider,
        recipe_service=resolved_recipe_service,
    )
    return service, resolved_uow, resolved_collection_uow, resolved_store
