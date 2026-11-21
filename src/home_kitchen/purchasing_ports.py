"""Protocol ports for inventory, store-trip picking, and trip-completion reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from home_kitchen.basket.models import SelectedProduct
from home_kitchen.shopping import PurchasePlanPort, ShoppingListPort

if TYPE_CHECKING:
    from home_kitchen.history import HistoryEntryCreate
    from home_kitchen.purchasing_reconciliation import CompletedTrip

__all__ = [
    "HistoryRecordPort",
    "OdaStatusPort",
    "PurchasePlanPort",
    "ShoppingListPort",
    "StoreTripRepository",
    "TripCompletionSession",
    "TripCompletionUnitOfWork",
    "TripReviewSources",
]


class HistoryRecordPort(Protocol):
    """Narrow port for recording domain events, optionally inside an active transaction."""

    def record(
        self,
        data: HistoryEntryCreate,
        actor_id: str | None = None,
        connection: Any = None,
    ) -> Any: ...


class OdaStatusPort(Protocol):
    """Narrow port for querying whether an Oda account is connected for an actor."""

    def status(self, actor_id: str) -> Mapping[str, Any]: ...


class StoreTripRepository(Protocol):
    """Port for reading and persisting in-store trip picking progress."""

    def load_checked_quantities(self, start: date, end: date) -> dict[UUID, Decimal]: ...

    def save_check(
        self,
        *,
        start: date,
        end: date,
        need_id: UUID,
        quantity: Decimal,
        history_entry: HistoryEntryCreate,
    ) -> None: ...


class TripReviewSources(BaseModel):
    """Active captures, regulars, and reviewed basket selections for trip completion."""

    captures: dict[UUID, dict[str, Any]]
    regulars: dict[UUID, dict[str, Any]]
    selected_products: dict[UUID, list[SelectedProduct]]


class TripCompletionSession(Protocol):
    """Transactional session for locked trip-completion review and persistence."""

    def load_review_sources(self, start: date, end: date) -> TripReviewSources: ...

    def get_completed_trip(self, idempotency_key: UUID) -> tuple[str, Any] | None: ...

    def current_timestamp(self) -> datetime: ...

    def save_completed_trip(self, trip: CompletedTrip, *, request_hash: str) -> None: ...

    def save_source_closures(
        self,
        *,
        start: date,
        end: date,
        trip_id: UUID,
        actor_id: str | None,
        closures: dict[tuple[UUID, str, UUID], str],
    ) -> None: ...

    def mark_captures_completed(self, capture_ids: set[UUID]) -> None: ...

    def update_regulars_last_purchased(
        self, regular_ids: set[UUID], purchased_on: date
    ) -> None: ...

    def record_history(self, entry: HistoryEntryCreate) -> None: ...


class TripCompletionUnitOfWork(Protocol):
    """Port for atomic trip-completion review, completion, and history listing."""

    def session(self) -> AbstractContextManager[TripCompletionSession]: ...

    def list_completed_trips(self, limit: int) -> list[CompletedTrip]: ...
