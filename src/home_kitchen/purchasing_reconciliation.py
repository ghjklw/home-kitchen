"""Explicit paid purchases, separate from picking, planning and stock counts."""

import builtins
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from home_kitchen.basket.models import ProductSnapshot, SelectedProduct
from home_kitchen.context import history_context
from home_kitchen.history import HistoryEntryCreate
from home_kitchen.models import NonBlank
from home_kitchen.purchasing_ports import (
    HistoryRecordPort,
    PurchasePlanPort,
    TripCompletionSession,
    TripCompletionUnitOfWork,
)
from home_kitchen.shopping import PurchasePlanLine
from home_kitchen.units import add_exact, canonicalize_quantity


class ReconciliationSource(BaseModel):
    id: UUID
    quantity: Decimal
    unit: str
    regular_id: UUID | None = None


class TripReviewLine(PurchasePlanLine):
    captures: list[ReconciliationSource] = Field(default_factory=list)
    regulars: list[ReconciliationSource] = Field(default_factory=list)
    selected_products: list[SelectedProduct] = Field(default_factory=list)


class TripCompletionReview(BaseModel):
    start: date
    end: date
    review_token: str
    lines: list[TripReviewLine]


class PurchasedProductCreate(BaseModel):
    """Reference a product already saved in this need's current basket review."""

    provider: NonBlank
    product_id: NonBlank
    packages: int = Field(gt=0, le=99)


class PurchasedLineCreate(BaseModel):
    need_id: UUID
    quantity: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None = None
    unit: NonBlank | None = None
    product: PurchasedProductCreate | None = None
    close_capture_ids: list[UUID] = Field(default_factory=list)
    regular_ids: list[UUID] = Field(default_factory=list)
    close_meal_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_sources(self):
        if (self.quantity is None) != (self.unit is None):
            raise ValueError("quantity and unit must be supplied together")
        if self.product is not None and self.quantity is not None:
            raise ValueError(
                "provide either an actual quantity or an exact product and package count"
            )
        for values in (self.close_capture_ids, self.regular_ids, self.close_meal_ids):
            if len(values) != len(set(values)):
                raise ValueError("Reconciled source IDs must be unique")
        return self


class SourceClosureCreate(BaseModel):
    """Close one reviewed shopping contribution without claiming a purchase."""

    need_id: UUID
    source_kind: Literal["meal", "capture", "regular"]
    source_id: UUID
    resolution: Literal["already_have", "not_needed", "unavailable", "other"]


class TripCompletionCreate(BaseModel):
    start: date
    end: date
    review_token: NonBlank
    reviewed: Literal[True]
    idempotency_key: UUID
    purchased_on: date
    purchases: list[PurchasedLineCreate] = Field(default_factory=list, max_length=500)
    source_closures: list[SourceClosureCreate] = Field(default_factory=list, max_length=1000)

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
        if not self.purchases and not self.source_closures:
            raise ValueError("Confirm at least one purchase or shopping-source resolution")
        closed = [
            (closure.need_id, closure.source_kind, closure.source_id)
            for closure in self.source_closures
        ]
        purchased = [
            (purchase.need_id, kind, source_id)
            for purchase in self.purchases
            for kind, identities in (
                ("capture", purchase.close_capture_ids),
                ("regular", purchase.regular_ids),
                ("meal", purchase.close_meal_ids),
            )
            for source_id in identities
        ]
        all_sources = closed + purchased
        if len(all_sources) != len(set(all_sources)):
            raise ValueError("A shopping source can be resolved only once per trip")
        return self


class CompletedTripLine(BaseModel):
    need: TripReviewLine
    purchase: PurchasedLineCreate
    product_snapshot: ProductSnapshot | None = None
    actual_quantity: Decimal | None = None
    actual_unit: str | None = None


class CompletedTrip(BaseModel):
    id: UUID
    idempotency_key: UUID
    start: date
    end: date
    purchased_on: date
    completed_at: datetime
    actor_id: str | None
    lines: list[CompletedTripLine]
    source_closures: list[SourceClosureCreate] = Field(default_factory=list)


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class TripCompletionService:
    def __init__(
        self,
        inventory: PurchasePlanPort,
        history: HistoryRecordPort | None = None,
        *,
        uow: TripCompletionUnitOfWork | None = None,
        engine: Any = None,
    ):
        self.inventory = inventory
        self.history = history
        self.engine = (
            engine
            if engine is not None
            else getattr(uow, "engine", None) or getattr(history, "engine", None)
        )
        self._uow = uow

    def _resolve_uow(self) -> TripCompletionUnitOfWork:
        if self._uow is None:
            from home_kitchen.purchasing_adapters import PostgresTripCompletionUnitOfWork

            self._uow = PostgresTripCompletionUnitOfWork(self.engine, self.history)
        return self._uow

    def review(self, start: date, end: date) -> TripCompletionReview:
        # Locks give one coherent view even though the existing plan builder owns
        # its read connections. Source mutations take ordinary conflicting locks.
        with self._resolve_uow().session() as session:
            return self._review(start, end, session)

    def _review(
        self, start: date, end: date, session: TripCompletionSession
    ) -> TripCompletionReview:
        plan = self.inventory.plan(start, end)
        sources = session.load_review_sources(start, end)
        captures = sources.captures
        regulars = sources.regulars
        selected_products = sources.selected_products
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
                    **need.model_dump(),
                    captures=capture_sources,
                    regulars=regular_sources,
                    selected_products=selected_products.get(need.id, []),
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
        with self._resolve_uow().session() as session:
            previous = session.get_completed_trip(data.idempotency_key)
            if previous is not None:
                prev_hash, prev_result = previous
                if prev_hash != fingerprint:
                    raise ValueError("Idempotency key already belongs to another purchase request")
                return CompletedTrip.model_validate(prev_result)
            review = self._review(data.start, data.end, session)
            if review.review_token != data.review_token:
                raise ValueError("Shopping needs changed; refresh and review the purchase again")
            current = {line.id: line for line in review.lines}
            snapshots: list[CompletedTripLine] = []
            closures: dict[tuple[UUID, str, UUID], str] = {}
            purchased_regular_ids: set[UUID] = set()
            for purchase in data.purchases:
                need = current.get(purchase.need_id)
                if need is None:
                    raise ValueError("Purchased need is no longer current; refresh the review")
                product_snapshot = None
                actual_quantity = purchase.quantity
                actual_unit = purchase.unit
                source_coverage_quantity = actual_quantity
                source_coverage_unit = actual_unit
                if purchase.product is not None:
                    selected = next(
                        (
                            selection.product
                            for selection in need.selected_products
                            if selection.product.provider == purchase.product.provider
                            and selection.product.product_id == purchase.product.product_id
                        ),
                        None,
                    )
                    if selected is None:
                        raise ValueError(
                            "Purchased product is not among the products reviewed for this need"
                        )
                    product_snapshot = selected
                    actual_quantity = selected.pack_quantity * purchase.product.packages
                    actual_unit = selected.pack_unit
                    _, need_unit = canonicalize_quantity(need.to_buy_quantity, need.unit)
                    if need_unit == "pack":
                        # A selected SKU package count can cover a pack-count need,
                        # while the recorded purchase remains in its physical unit.
                        source_coverage_quantity = Decimal(purchase.product.packages)
                        source_coverage_unit = "pack"
                self._validate_purchase_sources(
                    purchase,
                    need,
                    actual_quantity=actual_quantity,
                    actual_unit=actual_unit,
                    source_coverage_quantity=source_coverage_quantity,
                    source_coverage_unit=source_coverage_unit,
                )
                snapshots.append(
                    CompletedTripLine(
                        need=need,
                        purchase=purchase,
                        product_snapshot=product_snapshot,
                        actual_quantity=actual_quantity,
                        actual_unit=actual_unit,
                    )
                )
                for kind, source_ids in self._purchase_source_ids(purchase).items():
                    for source_id in source_ids:
                        closures[(need.id, kind, source_id)] = "purchased"
                purchased_regular_ids.update(purchase.regular_ids)
            for source_closure in data.source_closures:
                need = current.get(source_closure.need_id)
                if need is None:
                    raise ValueError("Shopping source is no longer current; refresh the review")
                self._require_source(need, source_closure.source_kind, source_closure.source_id)
                closures[(need.id, source_closure.source_kind, source_closure.source_id)] = (
                    source_closure.resolution
                )
                if source_closure.source_kind == "regular":
                    linked_capture = next(
                        (
                            item
                            for item in need.captures
                            if item.regular_id == source_closure.source_id
                        ),
                        None,
                    )
                    if linked_capture is not None:
                        closures[(need.id, "capture", linked_capture.id)] = (
                            source_closure.resolution
                        )

            completed_at = session.current_timestamp()
            trip = CompletedTrip(
                id=uuid4(),
                idempotency_key=data.idempotency_key,
                start=data.start,
                end=data.end,
                purchased_on=data.purchased_on,
                completed_at=completed_at,
                actor_id=actor,
                lines=snapshots,
                source_closures=data.source_closures,
            )
            session.save_completed_trip(trip, request_hash=fingerprint)
            session.save_source_closures(
                start=data.start,
                end=data.end,
                trip_id=trip.id,
                actor_id=actor,
                closures=closures,
            )
            captured_ids = {
                source_id
                for (_, kind, source_id), _resolution in closures.items()
                if kind == "capture"
            }
            session.mark_captures_completed(captured_ids)
            session.update_regulars_last_purchased(purchased_regular_ids, data.purchased_on)
            for line in trip.lines:
                session.record_history(
                    HistoryEntryCreate(
                        channel=history_context.get().channel,
                        kind="purchase",
                        message=(
                            f"Purchased {line.need.name}"
                            if line.actual_quantity is not None
                            else f"Purchased {line.need.name} (quantity unknown)"
                        ),
                        structured={
                            "action": "in_store_purchase",
                            "trip_id": str(trip.id),
                            "purchased_on": str(data.purchased_on),
                            **line.model_dump(mode="json"),
                        },
                    )
                )
            for source_closure in data.source_closures:
                session.record_history(
                    HistoryEntryCreate(
                        channel=history_context.get().channel,
                        kind="domain_event",
                        message=f"Resolved {source_closure.source_kind} shopping source",
                        structured={
                            "action": "shopping_source_resolved",
                            "trip_id": str(trip.id),
                            "week_start": data.start.isoformat(),
                            "week_end": data.end.isoformat(),
                            **source_closure.model_dump(mode="json"),
                        },
                    )
                )
        return trip

    @staticmethod
    def _purchase_source_ids(purchase: PurchasedLineCreate) -> dict[str, list[UUID]]:
        return {
            "capture": purchase.close_capture_ids,
            "regular": purchase.regular_ids,
            "meal": purchase.close_meal_ids,
        }

    @staticmethod
    def _require_source(need: TripReviewLine, kind: str, identity: UUID):
        source = next(
            (
                item
                for item in need.source_quantities
                if item.kind == kind and item.source_id == identity
            ),
            None,
        )
        # A confirmed regular suggestion is represented in this week's plan by
        # its capture. Keep the regular ID available for explicit purchase
        # history and last-purchased updates without counting its quantity twice.
        if source is None and kind == "regular":
            linked_capture = next(
                (item for item in need.captures if item.regular_id == identity), None
            )
            if linked_capture is not None:
                return linked_capture
        if source is None:
            if kind == "regular":
                raise ValueError("Regular item has no explicit provenance in this need")
            raise ValueError("Shopping source is not active for this need")
        return source

    @classmethod
    def _validate_purchase_sources(
        cls,
        purchase: PurchasedLineCreate,
        need: TripReviewLine,
        *,
        actual_quantity: Decimal | None,
        actual_unit: str | None,
        source_coverage_quantity: Decimal | None = None,
        source_coverage_unit: str | None = None,
    ) -> None:
        refs = [
            (kind, source_id)
            for kind, identities in cls._purchase_source_ids(purchase).items()
            for source_id in identities
        ]
        sources = [cls._require_source(need, kind, identity) for kind, identity in refs]
        if actual_quantity is None:
            return
        assert actual_unit is not None
        coverage_quantity, unit = canonicalize_quantity(
            source_coverage_quantity if source_coverage_quantity is not None else actual_quantity,
            source_coverage_unit if source_coverage_unit is not None else actual_unit,
        )
        if unit != need.unit:
            raise ValueError("Purchased unit must match the need's explicit unit family")

        # Allocate known stock to contributions in the stable plan order. This
        # lets a partial source closure be checked against the amount still due.
        stock = need.available_quantity
        due_by_source: dict[tuple[str, UUID], Decimal] = {}
        for source in need.source_quantities:
            quantity, source_unit = canonicalize_quantity(source.quantity, source.unit)
            if source_unit != unit:
                raise ValueError("Shopping source unit does not match the purchase")
            covered = min(quantity, stock)
            stock = add_exact(stock, -covered)
            due_by_source[(source.kind, source.source_id)] = add_exact(quantity, -covered)
        selected_due = Decimal(0)
        counted_sources: set[tuple[str, UUID]] = set()
        for (kind, identity), source in zip(refs, sources, strict=True):
            source_quantity, source_unit = canonicalize_quantity(source.quantity, source.unit)
            if source_unit != unit:
                raise ValueError("Shopping source unit does not match the purchase")
            quantity_key = (kind, identity)
            if quantity_key not in due_by_source and kind == "regular":
                linked_capture = next(
                    (item for item in need.captures if item.regular_id == identity), None
                )
                if linked_capture is not None:
                    quantity_key = ("capture", linked_capture.id)
            if quantity_key in counted_sources:
                continue
            counted_sources.add(quantity_key)
            selected_due = add_exact(selected_due, due_by_source[quantity_key])
        if selected_due > coverage_quantity:
            raise ValueError("Actual purchase does not fully cover the selected sources")

    def list(self, limit: int = 30) -> builtins.list[CompletedTrip]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return self._resolve_uow().list_completed_trips(limit)
