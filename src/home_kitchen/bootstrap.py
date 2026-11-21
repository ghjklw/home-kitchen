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

__all__ = [
    "_build_purchase_adapter",
    "create_basket_service",
    "create_in_memory_basket_service",
    "create_in_memory_store_trip_service",
    "create_in_memory_trip_completion_service",
    "create_inventory_service",
    "create_store_trip_service",
    "create_trip_completion_service",
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
