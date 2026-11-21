"""Secondary ports for basket matching and persistence."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from home_kitchen.catalogue.identity import IdentityCorrectionCitation
from home_kitchen.shopping import PurchasePlanPort

if TYPE_CHECKING:
    from home_kitchen.history import HistoryEntryCreate

__all__ = [
    "BasketHistoryPort",
    "BasketSelectionSession",
    "BasketSelectionStore",
    "IdentityCorrectionPort",
    "IdentityLookupPort",
    "PurchasePlanPort",
    "SkuPreferencePort",
]


class BasketHistoryPort(Protocol):
    """Port for recording basket domain events inside a selection transaction."""

    def record(
        self,
        data: HistoryEntryCreate,
        actor_id: str | None = None,
        connection: Any = None,
    ) -> Any: ...


class BasketSelectionSession(Protocol):
    """Transactional session for locked basket selection reads and writes."""

    @property
    def connection(self) -> Any: ...

    def acquire_need_locks(self, start: date, end: date, need_ids: Sequence[UUID]) -> None: ...

    def load_selections(self, start: date, end: date) -> list[dict[str, Any]]: ...

    def load_existing_payloads(
        self, start: date, end: date
    ) -> dict[tuple[UUID, str, str], Any]: ...

    def get_selection_payload(
        self, start: date, end: date, selection_id: UUID
    ) -> dict[str, Any] | None: ...

    def upsert_selection(
        self,
        *,
        selection_id: UUID,
        start: date,
        end: date,
        need_id: UUID,
        provider: str,
        product_id: str,
        payload: dict[str, Any],
        updated_at: datetime,
    ) -> None: ...

    def delete_selection_by_key(
        self, start: date, end: date, need_id: UUID, provider: str, product_id: str
    ) -> None: ...

    def delete_selection_by_id(self, selection_id: UUID) -> None: ...

    def record_history(self, event: HistoryEntryCreate) -> None: ...


class BasketSelectionStore(Protocol):
    """Port for loading and transactionally updating persisted basket selections."""

    def load_selections(
        self, start: date, end: date, *, connection: Any = None
    ) -> list[dict[str, Any]]: ...

    def session(self) -> AbstractContextManager[BasketSelectionSession]: ...


class SkuPreferencePort(Protocol):
    """Port for reading preferred and excluded provider SKUs for a canonical product."""

    def get_sku_preferences(self, product_id: UUID) -> tuple[list[str], list[str]]: ...


class IdentityLookupPort(Protocol):
    """Port for resolving a purchase need name to a canonical product ID."""

    def resolve_product_id(self, name: str) -> UUID | None: ...


class IdentityCorrectionPort(Protocol):
    """Port for retrieving identity correction citations for a purchase need."""

    def get_identity_corrections(
        self, need_name: str, product_id: UUID | None
    ) -> list[IdentityCorrectionCitation]: ...
