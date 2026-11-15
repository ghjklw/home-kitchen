"""Explicit paid purchases, separate from picking, planning and stock counts."""

import builtins
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection

from home_kitchen.context import history_context
from home_kitchen.history import HistoryEntryCreate, HistoryRepository
from home_kitchen.inventory import InventoryService, PurchasePlanLine
from home_kitchen.models import NonBlank
from home_kitchen.units import add_exact, canonicalize_quantity


class ReconciliationSource(BaseModel):
    id: UUID
    quantity: Decimal
    unit: str
    regular_id: UUID | None = None


class TripReviewLine(PurchasePlanLine):
    captures: list[ReconciliationSource] = Field(default_factory=list)
    regulars: list[ReconciliationSource] = Field(default_factory=list)


class TripCompletionReview(BaseModel):
    start: date
    end: date
    review_token: str
    lines: list[TripReviewLine]


class PurchasedLineCreate(BaseModel):
    need_id: UUID
    quantity: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    unit: NonBlank
    close_capture_ids: list[UUID] = Field(default_factory=list)
    regular_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_sources(self):
        for values in (self.close_capture_ids, self.regular_ids):
            if len(values) != len(set(values)):
                raise ValueError("Reconciled source IDs must be unique")
        return self


class TripCompletionCreate(BaseModel):
    start: date
    end: date
    review_token: NonBlank
    reviewed: Literal[True]
    idempotency_key: UUID
    purchased_on: date
    purchases: list[PurchasedLineCreate] = Field(min_length=1, max_length=500)

    @field_validator("purchased_on")
    @classmethod
    def actual_date(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("A purchase cannot be dated in the future")
        return value

    @model_validator(mode="after")
    def unique_needs(self):
        if self.end < self.start:
            raise ValueError("end must be on or after start")
        if len({line.need_id for line in self.purchases}) != len(self.purchases):
            raise ValueError("Each purchased need must appear once")
        return self


class CompletedTripLine(BaseModel):
    need: TripReviewLine
    purchase: PurchasedLineCreate


class CompletedTrip(BaseModel):
    id: UUID
    idempotency_key: UUID
    start: date
    end: date
    purchased_on: date
    completed_at: datetime
    actor_id: str | None
    lines: list[CompletedTripLine]


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class TripCompletionService:
    def __init__(self, inventory: InventoryService, history: HistoryRepository):
        self.inventory = inventory
        self.history = history
        self.engine = history.engine

    def review(self, start: date, end: date) -> TripCompletionReview:
        # Locks give one coherent view even though the existing plan builder owns
        # its read connections. Source mutations take ordinary conflicting locks.
        with self.engine.begin() as connection:
            self._lock(connection)
            return self._review(start, end, connection)

    @staticmethod
    def _lock(connection: Connection) -> None:
        connection.execute(
            text(
                "LOCK TABLE recipes, meals, items, regulars, confirmations, kitchen_inventory "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
        )

    def _review(self, start: date, end: date, connection: Connection) -> TripCompletionReview:
        plan = self.inventory.plan(start, end)
        captures = {
            row["id"]: dict(row)
            for row in connection.execute(
                text(
                    "SELECT i.id,i.quantity,i.unit,c.regular_id FROM items i LEFT JOIN confirmations c "
                    "ON c.item_id=i.id WHERE NOT i.completed ORDER BY i.id"
                )
            ).mappings()
        }
        regulars = {
            row["id"]: dict(row)
            for row in connection.execute(
                text("SELECT id,quantity,unit,last_purchased FROM regulars ORDER BY id")
            ).mappings()
        }
        lines = []
        for need in sorted(plan.purchase_lines, key=lambda line: str(line.id)):
            capture_sources, regular_sources = [], []
            for origin in sorted(need.origins):
                kind, _, identity = origin.partition(":")
                if kind == "item" and UUID(identity) in captures:
                    capture_sources.append(ReconciliationSource(**captures[UUID(identity)]))
                elif kind == "regular" and UUID(identity) in regulars:
                    regular_sources.append(ReconciliationSource(**regulars[UUID(identity)]))
            lines.append(
                TripReviewLine(
                    **need.model_dump(), captures=capture_sources, regulars=regular_sources
                )
            )
        payload = {
            "start": str(start),
            "end": str(end),
            "actor_id": history_context.get().actor_id,
            "lines": [line.model_dump(mode="json") for line in lines],
            # Dates affect whether a traced regular purchase is still current.
            "regulars": [
                {
                    **row,
                    "id": str(row["id"]),
                    "quantity": str(row["quantity"]),
                    "last_purchased": str(row["last_purchased"]),
                }
                for row in regulars.values()
            ],
        }
        return TripCompletionReview(start=start, end=end, lines=lines, review_token=_hash(payload))

    def complete(self, data: TripCompletionCreate) -> CompletedTrip:
        actor = history_context.get().actor_id
        fingerprint = _hash({"request": data.model_dump(mode="json"), "actor_id": actor})
        with self.engine.begin() as connection:
            self._lock(connection)
            previous = (
                connection.execute(
                    text(
                        "SELECT request_hash,result FROM kitchen_completed_trips WHERE idempotency_key=:key"
                    ),
                    {"key": data.idempotency_key},
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise ValueError("Idempotency key already belongs to another purchase request")
                return CompletedTrip.model_validate(previous["result"])
            review = self._review(data.start, data.end, connection)
            if review.review_token != data.review_token:
                raise ValueError("Shopping needs changed; refresh and review the purchase again")
            current = {line.id: line for line in review.lines}
            snapshots = []
            for purchase in data.purchases:
                need = current.get(purchase.need_id)
                if need is None:
                    raise ValueError("Purchased need is no longer current; refresh the review")
                self._reconcile(purchase, need, data.purchased_on, connection)
                snapshots.append(CompletedTripLine(need=need, purchase=purchase))
            completed_at = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
            trip = CompletedTrip(
                id=uuid4(),
                idempotency_key=data.idempotency_key,
                start=data.start,
                end=data.end,
                purchased_on=data.purchased_on,
                completed_at=completed_at,
                actor_id=actor,
                lines=snapshots,
            )
            connection.execute(
                text(
                    "INSERT INTO kitchen_completed_trips "
                    "(id,idempotency_key,request_hash,result,completed_at,actor_id) "
                    "VALUES (:id,:key,:hash,CAST(:result AS jsonb),:at,:actor)"
                ),
                {
                    "id": trip.id,
                    "key": data.idempotency_key,
                    "hash": fingerprint,
                    "result": trip.model_dump_json(),
                    "at": completed_at,
                    "actor": actor,
                },
            )
            for line in trip.lines:
                self.history.record(
                    HistoryEntryCreate(
                        channel=history_context.get().channel,
                        kind="purchase",
                        message=f"Purchased {line.need.name}",
                        structured={
                            "action": "in_store_purchase",
                            "trip_id": str(trip.id),
                            "purchased_on": str(data.purchased_on),
                            **line.model_dump(mode="json"),
                        },
                    ),
                    connection=connection,
                )
        return trip

    @staticmethod
    def _reconcile(
        purchase: PurchasedLineCreate,
        need: TripReviewLine,
        purchased_on: date,
        connection: Connection,
    ) -> None:
        actual, unit = canonicalize_quantity(purchase.quantity, purchase.unit)
        if unit != need.unit:
            raise ValueError("Purchased unit must match the need's explicit unit family")
        captures = {source.id: source for source in need.captures}
        regulars = {source.id: source for source in need.regulars}
        for source in need.captures:
            if source.regular_id:
                regulars[source.regular_id] = source
        total = Decimal(0)
        traced = set()
        for identity in purchase.close_capture_ids:
            source = captures.get(identity)
            if source is None:
                raise ValueError("Capture is not an active source of this need")
            quantity, source_unit = canonicalize_quantity(source.quantity, source.unit)
            if source_unit != unit:
                raise ValueError("Capture unit does not match the purchase")
            total = add_exact(total, quantity)
            if source.regular_id:
                traced.add(source.regular_id)
        for identity in purchase.regular_ids:
            source = regulars.get(identity)
            if source is None:
                raise ValueError("Regular item has no explicit provenance in this need")
            if identity not in traced:
                quantity, source_unit = canonicalize_quantity(source.quantity, source.unit)
                if source_unit != unit:
                    raise ValueError("Regular unit does not match the purchase")
                total = add_exact(total, quantity)
        if total > actual:
            raise ValueError("Actual purchase does not fully cover the selected sources")
        for identity in purchase.close_capture_ids:
            connection.execute(
                text("UPDATE items SET completed=true WHERE id=:id"), {"id": identity}
            )
        for identity in purchase.regular_ids:
            connection.execute(
                text(
                    "UPDATE regulars SET last_purchased=GREATEST(last_purchased,:day) WHERE id=:id"
                ),
                {"id": identity, "day": purchased_on},
            )

    def list(self, limit: int = 30) -> builtins.list[CompletedTrip]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT result FROM kitchen_completed_trips ORDER BY completed_at DESC,id DESC "
                    "LIMIT :limit"
                ),
                {"limit": limit},
            ).scalars()
            return [CompletedTrip.model_validate(row) for row in rows]
