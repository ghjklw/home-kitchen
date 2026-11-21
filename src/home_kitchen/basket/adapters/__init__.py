"""Adapters connecting basket matching and persistence ports to concrete implementations."""

from __future__ import annotations

import copy
import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from home_kitchen.basket.ports import (
    BasketHistoryPort,
    BasketSelectionSession,
    BasketSelectionStore,
    IdentityCorrectionPort,
    IdentityLookupPort,
    PurchasePlanPort,
    SkuPreferencePort,
)
from home_kitchen.catalogue.identity import IdentityCorrectionCitation, IdentityCorrectionEvent
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.catalogue.overview_service import ProductOverviewService

if TYPE_CHECKING:
    from home_kitchen.history import HistoryEntryCreate


class ProductOverviewSkuPreferenceAdapter(SkuPreferencePort):
    """Adapts ProductOverviewService preferences to SkuPreferencePort."""

    def __init__(self, product_overview: ProductOverviewService) -> None:
        self.product_overview = product_overview

    def get_sku_preferences(self, product_id: UUID) -> tuple[list[str], list[str]]:
        try:
            pref, exc = self.product_overview.get_preferences(product_id)
            return ([p.oda_product_id for p in pref], [e.oda_product_id for e in exc])
        except KeyError:
            return ([], [])


class CanonicalIdentityLookupAdapter(IdentityLookupPort):
    """Adapts CanonicalIdentityService resolution to IdentityLookupPort."""

    def __init__(
        self,
        identities: CanonicalIdentityService,
        *,
        household_id: UUID | None = None,
    ) -> None:
        self.identities = identities
        self.household_id = household_id

    def resolve_product_id(self, name: str) -> UUID | None:
        for loc in ("nb", "en", "fr"):
            for ctx in ("recipe", "receipt", ""):
                try:
                    res = self.identities.resolve(
                        "product", loc, ctx, name, household_id=self.household_id
                    )
                except TypeError:
                    res = self.identities.resolve("product", loc, ctx, name)
                if res.status == "resolved" and len(res.candidates) == 1:
                    return res.candidates[0].id
        return None


class CanonicalIdentityCorrectionAdapter(IdentityCorrectionPort):
    """Adapts CanonicalIdentityService correction audit events to IdentityCorrectionPort."""

    def __init__(
        self,
        identities: CanonicalIdentityService,
        *,
        household_id: UUID | None = None,
    ) -> None:
        self.identities = identities
        self.household_id = household_id

    def get_identity_corrections(
        self, need_name: str, product_id: UUID | None
    ) -> list[IdentityCorrectionCitation]:
        events = self.identities.list_correction_events(
            kind="product",
            alias=need_name,
            household_id=self.household_id,
            limit=10,
        )
        citations: list[IdentityCorrectionCitation] = []
        if isinstance(events, list):
            for ev in events:
                if not isinstance(ev, IdentityCorrectionEvent):
                    continue
                if product_id is not None and ev.new_identity_id != product_id:
                    continue
                citations.append(IdentityCorrectionCitation.from_event(ev))
        if not citations and product_id is not None:
            recent_events = self.identities.list_correction_events(
                kind="product",
                household_id=self.household_id,
                limit=50,
            )
            if isinstance(recent_events, list):
                for ev in recent_events:
                    if isinstance(ev, IdentityCorrectionEvent) and ev.new_identity_id == product_id:
                        citations.append(IdentityCorrectionCitation.from_event(ev))
        return citations


def _selection_lock_key(start: date, end: date, need_id: UUID) -> int:
    lock_id = uuid5(NAMESPACE_URL, f"basket-selection-lock:{start}:{end}:{need_id}").int >> 64
    return lock_id - (1 << 64) if lock_id >= (1 << 63) else lock_id


class PostgresBasketSelectionSession(BasketSelectionSession):
    """Transactional PostgreSQL session for locked basket selection reads and writes."""

    def __init__(self, connection: Connection, history: BasketHistoryPort) -> None:
        self._connection = connection
        self._history = history

    @property
    def connection(self) -> Connection:
        return self._connection

    def acquire_need_locks(self, start: date, end: date, need_ids: Sequence[UUID]) -> None:
        for need_id in sorted(set(need_ids), key=str):
            self._connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _selection_lock_key(start, end, need_id)},
            )

    def load_selections(self, start: date, end: date) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            text(
                "SELECT id, need_id, provider, product_id, payload "
                "FROM kitchen_basket_selections "
                "WHERE week_start = :week_start AND week_end = :week_end"
            ),
            {"week_start": start, "week_end": end},
        ).mappings()
        return [dict(row) for row in rows]

    def load_existing_payloads(self, start: date, end: date) -> dict[tuple[UUID, str, str], Any]:
        return {
            (row["need_id"], row["provider"], row["product_id"]): row["payload"]
            for row in self._connection.execute(
                text(
                    "SELECT need_id, provider, product_id, payload "
                    "FROM kitchen_basket_selections "
                    "WHERE week_start = :start AND week_end = :end"
                ),
                {"start": start, "end": end},
            ).mappings()
        }

    def get_selection_payload(
        self, start: date, end: date, selection_id: UUID
    ) -> dict[str, Any] | None:
        row = (
            self._connection.execute(
                text(
                    "SELECT payload FROM kitchen_basket_selections WHERE id = :id "
                    "AND week_start = :week_start AND week_end = :week_end"
                ),
                {"id": selection_id, "week_start": start, "week_end": end},
            )
            .mappings()
            .first()
        )
        return None if row is None else row["payload"]

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
    ) -> None:
        self._connection.execute(
            text(
                "INSERT INTO kitchen_basket_selections "
                "(id, week_start, week_end, need_id, provider, product_id, payload, updated_at) "
                "VALUES (:id, :week_start, :week_end, :need_id, :provider, :product_id, "
                "CAST(:payload AS jsonb), :updated_at) "
                "ON CONFLICT (week_start, week_end, need_id, provider, product_id) "
                "DO UPDATE SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at"
            ),
            {
                "id": selection_id,
                "week_start": start,
                "week_end": end,
                "need_id": need_id,
                "provider": provider,
                "product_id": product_id,
                "payload": json.dumps(payload),
                "updated_at": updated_at,
            },
        )

    def delete_selection_by_key(
        self, start: date, end: date, need_id: UUID, provider: str, product_id: str
    ) -> None:
        self._connection.execute(
            text(
                "DELETE FROM kitchen_basket_selections "
                "WHERE week_start = :start AND week_end = :end "
                "AND need_id = :need_id AND provider = :provider AND product_id = :product_id"
            ),
            {
                "start": start,
                "end": end,
                "need_id": need_id,
                "provider": provider,
                "product_id": product_id,
            },
        )

    def delete_selection_by_id(self, selection_id: UUID) -> None:
        self._connection.execute(
            text("DELETE FROM kitchen_basket_selections WHERE id = :id"),
            {"id": selection_id},
        )

    def record_history(self, event: HistoryEntryCreate) -> None:
        self._history.record(event, connection=self._connection)


class PostgresBasketSelectionStore(BasketSelectionStore):
    """PostgreSQL adapter persisting basket selections in `kitchen_basket_selections`."""

    def __init__(self, engine: Engine | None, history: BasketHistoryPort) -> None:
        self.engine = engine
        self.history = history

    def _require_engine(self) -> Engine:
        if self.engine is None:
            raise RuntimeError("BasketService requires a database engine")
        return self.engine

    def load_selections(
        self, start: date, end: date, *, connection: Any = None
    ) -> list[dict[str, Any]]:
        query = text(
            "SELECT id, need_id, provider, product_id, payload "
            "FROM kitchen_basket_selections "
            "WHERE week_start = :week_start AND week_end = :week_end"
        )
        params = {"week_start": start, "week_end": end}
        if connection is not None:
            return [dict(row) for row in connection.execute(query, params).mappings()]
        engine = self._require_engine()
        with engine.connect() as owned_connection:
            rows = owned_connection.execute(query, params).mappings()
            return [dict(row) for row in rows]

    @contextmanager
    def session(self) -> Generator[BasketSelectionSession]:
        engine = self._require_engine()
        with engine.begin() as connection:
            yield PostgresBasketSelectionSession(connection, self.history)


class MemoryBasketSelectionSession(BasketSelectionSession):
    """In-memory transactional session for `MemoryBasketSelectionStore`."""

    def __init__(self, store: MemoryBasketSelectionStore) -> None:
        self._store = store
        self._sentinel = object()

    @property
    def connection(self) -> Any:
        return self._sentinel

    def acquire_need_locks(self, start: date, end: date, need_ids: Sequence[UUID]) -> None:
        return None

    def load_selections(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._store.load_selections(start, end)

    def load_existing_payloads(self, start: date, end: date) -> dict[tuple[UUID, str, str], Any]:
        return {
            (row["need_id"], row["provider"], row["product_id"]): copy.deepcopy(row["payload"])
            for (week_start, week_end, _, _, _), row in self._store.rows.items()
            if week_start == start and week_end == end
        }

    def get_selection_payload(
        self, start: date, end: date, selection_id: UUID
    ) -> dict[str, Any] | None:
        for (week_start, week_end, _, _, _), row in self._store.rows.items():
            if week_start == start and week_end == end and row["id"] == selection_id:
                return copy.deepcopy(row["payload"])
        return None

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
    ) -> None:
        key = (start, end, need_id, provider, product_id)
        self._store.rows[key] = {
            "id": selection_id,
            "need_id": need_id,
            "provider": provider,
            "product_id": product_id,
            "payload": copy.deepcopy(payload),
            "updated_at": updated_at,
        }

    def delete_selection_by_key(
        self, start: date, end: date, need_id: UUID, provider: str, product_id: str
    ) -> None:
        self._store.rows.pop((start, end, need_id, provider, product_id), None)

    def delete_selection_by_id(self, selection_id: UUID) -> None:
        for key, row in list(self._store.rows.items()):
            if row["id"] == selection_id:
                self._store.rows.pop(key, None)

    def record_history(self, event: HistoryEntryCreate) -> None:
        if self._store.history is not None:
            self._store.history.record(event)
        self._store.recorded_events.append(event)


class MemoryBasketSelectionStore(BasketSelectionStore):
    """Pure in-memory selection store with copy-on-write rollback for unit tests."""

    def __init__(self, history: BasketHistoryPort | None = None) -> None:
        self.history = history
        self.rows: dict[tuple[date, date, UUID, str, str], dict[str, Any]] = {}
        self.recorded_events: list[HistoryEntryCreate] = []

    def load_selections(
        self, start: date, end: date, *, connection: Any = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "need_id": row["need_id"],
                "provider": row["provider"],
                "product_id": row["product_id"],
                "payload": copy.deepcopy(row["payload"]),
            }
            for (week_start, week_end, _, _, _), row in self.rows.items()
            if week_start == start and week_end == end
        ]

    @contextmanager
    def session(self) -> Generator[BasketSelectionSession]:
        rows_snap = copy.deepcopy(self.rows)
        events_snap = list(self.recorded_events)
        try:
            yield MemoryBasketSelectionSession(self)
        except Exception:
            self.rows = rows_snap
            self.recorded_events = events_snap
            raise


def build_legacy_basket_dependencies(
    kitchen_or_plan: Any,
    history: BasketHistoryPort,
    *,
    engine: Engine | None = None,
    selection_store: BasketSelectionStore | None = None,
) -> tuple[PurchasePlanPort, BasketSelectionStore, Engine | None]:
    """Adapt legacy `BasketService(kitchen, history)` callers to explicit ports."""
    resolved_engine = (
        engine
        if engine is not None
        else getattr(kitchen_or_plan, "engine", None)
        or getattr(getattr(kitchen_or_plan, "store", None), "engine", None)
        or getattr(history, "engine", None)
    )
    if callable(getattr(kitchen_or_plan, "plan", None)):
        plan_reader: PurchasePlanPort = kitchen_or_plan
    else:
        from home_kitchen.inventory import InventoryService

        plan_reader = InventoryService(kitchen_or_plan, history, engine=resolved_engine)
    store = selection_store or PostgresBasketSelectionStore(resolved_engine, history)
    return plan_reader, store, resolved_engine
