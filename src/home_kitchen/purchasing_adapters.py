"""PostgreSQL and in-memory adapters for store-trip and trip-completion ports."""

from __future__ import annotations

import copy
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from home_kitchen.basket.models import SelectedProduct
from home_kitchen.purchasing_ports import (
    HistoryRecordPort,
    StoreTripRepository,
    TripCompletionSession,
    TripCompletionUnitOfWork,
    TripReviewSources,
)

if TYPE_CHECKING:
    from home_kitchen.history import HistoryEntryCreate
    from home_kitchen.purchasing_reconciliation import CompletedTrip


class PostgresStoreTripRepository(StoreTripRepository):
    """PostgreSQL adapter persisting in-store checkoffs in `kitchen_store_trip`."""

    def __init__(self, engine: Engine | None, history: HistoryRecordPort | None = None) -> None:
        self.engine = engine
        self.history = history

    def _require_engine(self) -> Engine:
        if self.engine is None:
            raise RuntimeError("StoreTripService requires a database engine")
        return self.engine

    def load_checked_quantities(self, start: date, end: date) -> dict[UUID, Decimal]:
        engine = self._require_engine()
        with engine.connect() as connection:
            return dict(
                connection.execute(
                    text(
                        "SELECT need_id, checked_quantity FROM kitchen_store_trip "
                        "WHERE week_start = :start AND week_end = :end"
                    ),
                    {"start": start, "end": end},
                ).all()
            )  # type: ignore[arg-type]

    def save_check(
        self,
        *,
        start: date,
        end: date,
        need_id: UUID,
        quantity: Decimal,
        history_entry: HistoryEntryCreate,
    ) -> None:
        engine = self._require_engine()
        with engine.begin() as connection:
            changed = connection.execute(
                text(
                    "INSERT INTO kitchen_store_trip "
                    "(week_start, week_end, need_id, checked_quantity) "
                    "VALUES (:start, :end, :need, :quantity) "
                    "ON CONFLICT (week_start, week_end, need_id) DO UPDATE "
                    "SET checked_quantity = EXCLUDED.checked_quantity, updated_at = now() "
                    "WHERE kitchen_store_trip.checked_quantity IS DISTINCT FROM "
                    "EXCLUDED.checked_quantity RETURNING need_id"
                ),
                {"start": start, "end": end, "need": need_id, "quantity": quantity},
            ).scalar_one_or_none()
            if changed is not None and self.history is not None:
                self.history.record(history_entry, connection=connection)


class MemoryStoreTripRepository(StoreTripRepository):
    """Pure in-memory store-trip repository for unit tests without a database."""

    def __init__(self, history: HistoryRecordPort | None = None) -> None:
        self.history = history
        self.progress: dict[tuple[date, date, UUID], Decimal] = {}
        self.recorded_events: list[HistoryEntryCreate] = []

    def load_checked_quantities(self, start: date, end: date) -> dict[UUID, Decimal]:
        return {
            need_id: qty
            for (week_start, week_end, need_id), qty in self.progress.items()
            if week_start == start and week_end == end
        }

    def save_check(
        self,
        *,
        start: date,
        end: date,
        need_id: UUID,
        quantity: Decimal,
        history_entry: HistoryEntryCreate,
    ) -> None:
        key = (start, end, need_id)
        previous = self.progress.get(key)
        if previous == quantity:
            return
        if self.history is not None:
            self.history.record(history_entry)
        self.progress[key] = quantity
        self.recorded_events.append(history_entry)


class PostgresTripCompletionSession(TripCompletionSession):
    """Transactional PostgreSQL session for trip-completion review and writes."""

    def __init__(self, connection: Connection, history: HistoryRecordPort | None) -> None:
        self._connection = connection
        self._history = history

    def load_review_sources(self, start: date, end: date) -> TripReviewSources:
        captures = {
            row["id"]: dict(row)
            for row in self._connection.execute(
                text(
                    "SELECT i.id,i.quantity,i.unit,c.regular_id FROM items i LEFT JOIN confirmations c "
                    "ON c.item_id=i.id WHERE NOT i.completed ORDER BY i.id"
                )
            ).mappings()
        }
        regulars = {
            row["id"]: dict(row)
            for row in self._connection.execute(
                text("SELECT id,quantity,unit,last_purchased FROM regulars ORDER BY id")
            ).mappings()
        }
        selected_products: dict[UUID, list[SelectedProduct]] = {}
        for row in self._connection.execute(
            text(
                "SELECT need_id,payload FROM kitchen_basket_selections "
                "WHERE week_start=:start AND week_end=:end ORDER BY need_id,provider,product_id"
            ),
            {"start": start, "end": end},
        ).mappings():
            selected_products.setdefault(row["need_id"], []).append(
                SelectedProduct.model_validate(row["payload"])
            )
        return TripReviewSources(
            captures=captures,
            regulars=regulars,
            selected_products=selected_products,
        )

    def get_completed_trip(self, idempotency_key: UUID) -> tuple[str, Any] | None:
        previous = (
            self._connection.execute(
                text(
                    "SELECT request_hash,result FROM kitchen_completed_trips WHERE idempotency_key=:key"
                ),
                {"key": idempotency_key},
            )
            .mappings()
            .first()
        )
        if previous is None:
            return None
        return (previous["request_hash"], previous["result"])

    def current_timestamp(self) -> datetime:
        return self._connection.execute(text("SELECT clock_timestamp()")).scalar_one()

    def save_completed_trip(self, trip: CompletedTrip, *, request_hash: str) -> None:
        self._connection.execute(
            text(
                "INSERT INTO kitchen_completed_trips "
                "(id,idempotency_key,request_hash,result,completed_at,actor_id) "
                "VALUES (:id,:key,:hash,CAST(:result AS jsonb),:at,:actor)"
            ),
            {
                "id": trip.id,
                "key": trip.idempotency_key,
                "hash": request_hash,
                "result": trip.model_dump_json(),
                "at": trip.completed_at,
                "actor": trip.actor_id,
            },
        )

    def save_source_closures(
        self,
        *,
        start: date,
        end: date,
        trip_id: UUID,
        actor_id: str | None,
        closures: dict[tuple[UUID, str, UUID], str],
    ) -> None:
        for (need_id, source_kind, source_id), resolution in closures.items():
            self._connection.execute(
                text(
                    "INSERT INTO kitchen_trip_source_closures "
                    "(week_start,week_end,need_id,source_kind,source_id,resolution,trip_id,actor_id) "
                    "VALUES (:start,:end,:need,:kind,:source,:resolution,:trip,:actor)"
                ),
                {
                    "start": start,
                    "end": end,
                    "need": need_id,
                    "kind": source_kind,
                    "source": source_id,
                    "resolution": resolution,
                    "trip": trip_id,
                    "actor": actor_id,
                },
            )

    def mark_captures_completed(self, capture_ids: set[UUID]) -> None:
        for identity in capture_ids:
            self._connection.execute(
                text("UPDATE items SET completed=true WHERE id=:id"), {"id": identity}
            )

    def update_regulars_last_purchased(self, regular_ids: set[UUID], purchased_on: date) -> None:
        for identity in regular_ids:
            self._connection.execute(
                text(
                    "UPDATE regulars SET last_purchased=GREATEST(last_purchased,:day) WHERE id=:id"
                ),
                {"id": identity, "day": purchased_on},
            )

    def record_history(self, entry: HistoryEntryCreate) -> None:
        if self._history is not None:
            self._history.record(entry, connection=self._connection)


class PostgresTripCompletionUnitOfWork(TripCompletionUnitOfWork):
    """PostgreSQL unit of work for locked trip completion and audit queries."""

    def __init__(self, engine: Engine | None, history: HistoryRecordPort | None = None) -> None:
        self.engine = engine
        self.history = history

    def _require_engine(self) -> Engine:
        if self.engine is None:
            raise RuntimeError("TripCompletionService requires a database engine")
        return self.engine

    @contextmanager
    def session(self) -> Generator[TripCompletionSession]:
        engine = self._require_engine()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "LOCK TABLE recipes, meals, items, regulars, confirmations, kitchen_inventory, "
                    "kitchen_basket_selections, kitchen_trip_source_closures "
                    "IN SHARE ROW EXCLUSIVE MODE"
                )
            )
            yield PostgresTripCompletionSession(connection, self.history)

    def list_completed_trips(self, limit: int) -> list[CompletedTrip]:
        from home_kitchen.purchasing_reconciliation import CompletedTrip

        engine = self._require_engine()
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT result FROM kitchen_completed_trips ORDER BY completed_at DESC,id DESC "
                    "LIMIT :limit"
                ),
                {"limit": limit},
            ).scalars()
            return [CompletedTrip.model_validate(row) for row in rows]


class MemoryTripCompletionSession(TripCompletionSession):
    """In-memory session backing `MemoryTripCompletionUnitOfWork`."""

    def __init__(self, uow: MemoryTripCompletionUnitOfWork) -> None:
        self._uow = uow

    def load_review_sources(self, start: date, end: date) -> TripReviewSources:
        captures = {
            capture_id: copy.deepcopy(row)
            for capture_id, row in self._uow.captures.items()
            if not row.get("completed", False)
        }
        captures_out = {
            capture_id: {
                "id": capture_id,
                "quantity": row["quantity"],
                "unit": row["unit"],
                "regular_id": row.get("regular_id"),
            }
            for capture_id, row in sorted(captures.items(), key=lambda item: str(item[0]))
        }
        regulars_out = {
            regular_id: {
                "id": regular_id,
                "quantity": row["quantity"],
                "unit": row["unit"],
                "last_purchased": row.get("last_purchased"),
            }
            for regular_id, row in sorted(self._uow.regulars.items(), key=lambda item: str(item[0]))
        }
        selected = copy.deepcopy(self._uow.selected_products.get((start, end), {}))
        return TripReviewSources(
            captures=captures_out,
            regulars=regulars_out,
            selected_products=selected,
        )

    def get_completed_trip(self, idempotency_key: UUID) -> tuple[str, Any] | None:
        record = self._uow.completed_by_key.get(idempotency_key)
        if record is None:
            return None
        return (record[0], copy.deepcopy(record[1]))

    def current_timestamp(self) -> datetime:
        return datetime.now(UTC)

    def save_completed_trip(self, trip: CompletedTrip, *, request_hash: str) -> None:
        payload = trip.model_dump(mode="json")
        self._uow.completed_by_key[trip.idempotency_key] = (request_hash, payload)
        self._uow.completed_trips.append(payload)

    def save_source_closures(
        self,
        *,
        start: date,
        end: date,
        trip_id: UUID,
        actor_id: str | None,
        closures: dict[tuple[UUID, str, UUID], str],
    ) -> None:
        for (need_id, source_kind, source_id), resolution in closures.items():
            self._uow.source_closures.append(
                {
                    "week_start": start,
                    "week_end": end,
                    "need_id": need_id,
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "resolution": resolution,
                    "trip_id": trip_id,
                    "actor_id": actor_id,
                }
            )

    def mark_captures_completed(self, capture_ids: set[UUID]) -> None:
        for capture_id in capture_ids:
            if capture_id in self._uow.captures:
                self._uow.captures[capture_id]["completed"] = True

    def update_regulars_last_purchased(self, regular_ids: set[UUID], purchased_on: date) -> None:
        for regular_id in regular_ids:
            row = self._uow.regulars.get(regular_id)
            if row is not None:
                prev = row.get("last_purchased")
                row["last_purchased"] = purchased_on if prev is None else max(prev, purchased_on)

    def record_history(self, entry: HistoryEntryCreate) -> None:
        if self._uow.history is not None:
            self._uow.history.record(entry)
        self._uow.recorded_events.append(entry)


class MemoryTripCompletionUnitOfWork(TripCompletionUnitOfWork):
    """Pure in-memory unit of work with copy-on-write rollback for unit tests."""

    def __init__(self, history: HistoryRecordPort | None = None) -> None:
        self.history = history
        self.captures: dict[UUID, dict[str, Any]] = {}
        self.regulars: dict[UUID, dict[str, Any]] = {}
        self.selected_products: dict[tuple[date, date], dict[UUID, list[SelectedProduct]]] = {}
        self.completed_by_key: dict[UUID, tuple[str, dict[str, Any]]] = {}
        self.completed_trips: list[dict[str, Any]] = []
        self.source_closures: list[dict[str, Any]] = []
        self.recorded_events: list[HistoryEntryCreate] = []

    @contextmanager
    def session(self) -> Generator[TripCompletionSession]:
        captures_snap = copy.deepcopy(self.captures)
        regulars_snap = copy.deepcopy(self.regulars)
        selected_snap = copy.deepcopy(self.selected_products)
        by_key_snap = copy.deepcopy(self.completed_by_key)
        trips_snap = copy.deepcopy(self.completed_trips)
        closures_snap = copy.deepcopy(self.source_closures)
        events_snap = list(self.recorded_events)
        try:
            yield MemoryTripCompletionSession(self)
        except Exception:
            self.captures = captures_snap
            self.regulars = regulars_snap
            self.selected_products = selected_snap
            self.completed_by_key = by_key_snap
            self.completed_trips = trips_snap
            self.source_closures = closures_snap
            self.recorded_events = events_snap
            raise

    def list_completed_trips(self, limit: int) -> list[CompletedTrip]:
        from home_kitchen.purchasing_reconciliation import CompletedTrip

        ordered = sorted(
            self.completed_trips,
            key=lambda row: (row["completed_at"], row["id"]),
            reverse=True,
        )[:limit]
        return [CompletedTrip.model_validate(row) for row in ordered]
