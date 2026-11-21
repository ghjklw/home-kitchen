"""Bootstrap and dependency injection wiring for reviewed basket services."""

from __future__ import annotations

from typing import Any

from home_kitchen.basket.adapters import (
    MemoryBasketSelectionStore,
    PostgresBasketSelectionStore,
    build_legacy_basket_dependencies,
)
from home_kitchen.basket.ports import (
    BasketHistoryPort,
    BasketSelectionStore,
    PurchasePlanPort,
)
from home_kitchen.basket.repository import BasketService


def create_basket_service(
    plan_reader: PurchasePlanPort | Any,
    history: BasketHistoryPort,
    *,
    engine: Any = None,
    selection_store: BasketSelectionStore | None = None,
) -> BasketService:
    """Bootstrap a production `BasketService` wired via Protocol ports."""
    if selection_store is not None and callable(getattr(plan_reader, "plan", None)):
        return BasketService(
            plan_reader,
            history,
            selection_store=selection_store,
            engine=engine,
        )
    resolved_plan_reader, resolved_store, resolved_engine = build_legacy_basket_dependencies(
        plan_reader,
        history,
        engine=engine,
        selection_store=selection_store,
    )
    if not isinstance(resolved_store, PostgresBasketSelectionStore) and selection_store is None:
        resolved_store = PostgresBasketSelectionStore(resolved_engine, history)
    return BasketService(
        resolved_plan_reader,
        history,
        selection_store=resolved_store,
        engine=resolved_engine,
    )


def create_in_memory_basket_service(
    plan_reader: PurchasePlanPort,
    history: BasketHistoryPort | None = None,
    *,
    selection_store: MemoryBasketSelectionStore | None = None,
) -> tuple[BasketService, MemoryBasketSelectionStore]:
    """Bootstrap an in-memory `BasketService` for unit tests without a database."""
    store = selection_store if selection_store is not None else MemoryBasketSelectionStore(history)
    resolved_history: BasketHistoryPort = history if history is not None else _NoopHistoryRecorder()
    if store.history is None and history is not None:
        store.history = history
    service = BasketService(plan_reader, resolved_history, selection_store=store)
    return service, store


class _NoopHistoryRecorder:
    def record(self, data: Any, actor_id: str | None = None, connection: Any = None) -> None:
        return None
