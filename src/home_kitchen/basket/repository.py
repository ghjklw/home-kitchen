"""Persistence and validation for a reviewed, non-checkout shopping basket."""

import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import TypeAdapter

from home_kitchen.history import HistoryChannel, HistoryEntryCreate
from home_kitchen.models import ShoppingLine, WeekShopping
from home_kitchen.shopping import need_id
from home_kitchen.units import canonicalize_quantity

from .evaluation import evaluate_basket, selection_allocation
from .matching import NeedMatchResult
from .models import BasketReview, PurchaseNeed, SelectedProduct
from .ports import BasketHistoryPort, BasketSelectionStore, PurchasePlanPort

_selection_adapter = TypeAdapter(SelectedProduct)


def _selection_id(start: date, end: date, selection: SelectedProduct) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"basket:{start}:{end}:{selection.need_id}:{selection.product.provider}:"
        f"{selection.product.product_id}",
    )


def _unverified(selection: SelectedProduct) -> SelectedProduct:
    payload = selection.model_dump(mode="python")
    payload["product"]["evidence_status"] = "unverified"
    for alternative in payload["alternatives"]:
        alternative["evidence_status"] = "unverified"
    return _selection_adapter.validate_python(payload)


class BasketService:
    """Persist product choices and recompute review against current kitchen needs."""

    def __init__(
        self,
        purchase_plan: PurchasePlanPort | Any,
        history: BasketHistoryPort,
        *,
        selection_store: BasketSelectionStore | None = None,
        engine: Any = None,
    ):
        if selection_store is not None and callable(getattr(purchase_plan, "plan", None)):
            self.purchase_plan: PurchasePlanPort = purchase_plan
            self.selection_store: BasketSelectionStore = selection_store
            self.engine = engine if engine is not None else getattr(selection_store, "engine", None)
        else:
            from home_kitchen.basket.adapters import build_legacy_basket_dependencies

            plan_reader, store, resolved_engine = build_legacy_basket_dependencies(
                purchase_plan, history, engine=engine, selection_store=selection_store
            )
            self.purchase_plan = plan_reader
            self.selection_store = store
            self.engine = resolved_engine
        self.inventory = self.purchase_plan
        self.history = history

    def review(self, start: date, end: date) -> BasketReview:
        week = self._week(start, end)
        needs = self._needs(week, start, end)
        rows = self._load(start, end)
        valid_ids = {need.id for need in needs}
        active: list[SelectedProduct] = []
        stale_count = 0
        for row in rows:
            selection = _selection_adapter.validate_python(row["payload"])
            if selection.need_id not in valid_ids:
                stale_count += 1
                continue
            active.append(_unverified(selection))
        result = evaluate_basket(needs, active)
        result.unquantified = week.unquantified
        if week.unquantified:
            result.ready_for_review = False
            result.issues.extend(
                f"Quantity requires review: {need.name}" for need in week.unquantified
            )
        if stale_count:
            result.issues.append(
                f"{stale_count} saved product selection(s) no longer match current shopping needs"
            )
        return result

    def select(
        self,
        start: date,
        end: date,
        selection: SelectedProduct,
        *,
        channel: HistoryChannel = "api",
    ) -> BasketReview:
        week = self._week(start, end)
        needs = self._needs(week, start, end)
        need_by_id = {need.id: need for need in needs}
        if selection.need_id not in need_by_id:
            raise ValueError(f"selection references unknown need: {selection.need_id}")
        selection = _unverified(selection)
        need = need_by_id[selection.need_id]
        allocated = selection_allocation(need, selection)
        _, allocated_unit = canonicalize_quantity(need.quantity, need.unit)

        now = datetime.now(UTC)
        selection_id = _selection_id(start, end, selection)
        payload = selection.model_dump(mode="json")
        event = HistoryEntryCreate(
            channel=channel,
            kind="domain_event",
            message=f"Selected {selection.product.name} for {need.name}",
            structured={
                "action": "basket_product_selected",
                "week_start": start.isoformat(),
                "week_end": end.isoformat(),
                "need_id": str(selection.need_id),
                "provider": selection.product.provider,
                "product_id": selection.product.product_id,
                "packages": selection.packages,
                "pack_quantity": str(selection.product.pack_quantity),
                "pack_unit": selection.product.pack_unit,
                "allocated_quantity": str(allocated),
                "allocated_unit": allocated_unit,
                "need_quantity": str(need.quantity),
            },
        )
        with self.selection_store.session() as session:
            session.acquire_need_locks(start, end, [selection.need_id])
            existing_rows = self._load(start, end, connection=session.connection)
            valid_ids = set(need_by_id)
            existing = []
            for row in existing_rows:
                saved = _selection_adapter.validate_python(row["payload"])
                if saved.need_id not in valid_ids:
                    continue
                saved = _unverified(saved)
                if not (
                    saved.need_id == selection.need_id
                    and saved.product.provider == selection.product.provider
                    and saved.product.product_id == selection.product.product_id
                ):
                    existing.append(saved)
            # Evaluate the candidate in context before any persistent write. Evaluation
            # retains residual/overbuy quantities as review issues without guessing.
            evaluate_basket(needs, [*existing, selection])
            session.upsert_selection(
                selection_id=selection_id,
                start=start,
                end=end,
                need_id=selection.need_id,
                provider=selection.product.provider,
                product_id=selection.product.product_id,
                payload=payload,
                updated_at=now,
            )
            session.record_history(event)
        return self.review(start, end)

    def select_batch(
        self,
        start: date,
        end: date,
        selections: list[SelectedProduct],
        evidence: dict[UUID, NeedMatchResult],
        *,
        channel: HistoryChannel = "api",
    ) -> BasketReview:
        """Persist a reviewed set atomically, after rechecking server match evidence."""
        week = self._week(start, end)
        needs = self._needs(week, start, end)
        need_by_id = {need.id: need for need in needs}
        if not selections or len(selections) > 100:
            raise ValueError("Select between 1 and 100 products to save")
        if len({selection.need_id for selection in selections}) != len(selections):
            raise ValueError("Select at most one product for each purchase need")
        if set(evidence) != {selection.need_id for selection in selections}:
            raise ValueError("Product matches are stale. Refresh matches and try again.")

        safe: list[SelectedProduct] = []
        for selection in selections:
            need = need_by_id.get(selection.need_id)
            matched = evidence[selection.need_id]
            if (
                need is None
                or matched.need_id != need.id
                or matched.quantity != need.quantity
                or matched.unit.casefold() != need.unit.casefold()
            ):
                raise ValueError("Shopping needs changed. Refresh matches and try again.")
            if selection.product.pack_quantity <= 0:
                raise ValueError(
                    "Product package is unknown or incompatible with the current need."
                )
            selection_allocation(need, selection)
            safe.append(_unverified(selection))

        existing_rows = self._load(start, end)
        active: list[SelectedProduct] = []
        for row in existing_rows:
            saved = _selection_adapter.validate_python(row["payload"])
            if saved.need_id in need_by_id:
                active.append(_unverified(saved))
        submitted_need_ids = {selection.need_id for selection in safe}
        combined = [item for item in active if item.need_id not in submitted_need_ids] + safe
        evaluate_basket(needs, combined)

        now = datetime.now(UTC)
        with self.selection_store.session() as session:
            session.acquire_need_locks(start, end, sorted(submitted_need_ids, key=str))
            existing_payloads = session.load_existing_payloads(start, end)
            submitted_need_ids = {selection.need_id for selection in safe}
            desired_keys = {
                (selection.need_id, selection.product.provider, selection.product.product_id)
                for selection in safe
            }
            for key in existing_payloads:
                if key[0] in submitted_need_ids and key not in desired_keys:
                    session.delete_selection_by_key(start, end, key[0], key[1], key[2])
            for selection in safe:
                selection_id = _selection_id(start, end, selection)
                payload = selection.model_dump(mode="json")
                key = (selection.need_id, selection.product.provider, selection.product.product_id)
                previous = existing_payloads.get(key)
                if (
                    previous is not None
                    and (previous if isinstance(previous, dict) else json.loads(previous))
                    == payload
                ):
                    continue
                session.upsert_selection(
                    selection_id=selection_id,
                    start=start,
                    end=end,
                    need_id=selection.need_id,
                    provider=selection.product.provider,
                    product_id=selection.product.product_id,
                    payload=payload,
                    updated_at=now,
                )
                need = need_by_id[selection.need_id]
                allocated = selection_allocation(need, selection)
                _, allocated_unit = canonicalize_quantity(need.quantity, need.unit)
                session.record_history(
                    HistoryEntryCreate(
                        channel=channel,
                        kind="domain_event",
                        message=f"Selected {selection.product.name} for {need.name}",
                        structured={
                            "action": "basket_product_selected",
                            "week_start": start.isoformat(),
                            "week_end": end.isoformat(),
                            "need_id": str(selection.need_id),
                            "provider": selection.product.provider,
                            "product_id": selection.product.product_id,
                            "packages": selection.packages,
                            "pack_quantity": str(selection.product.pack_quantity),
                            "pack_unit": selection.product.pack_unit,
                            "allocated_quantity": str(allocated),
                            "allocated_unit": allocated_unit,
                            "need_quantity": str(need.quantity),
                        },
                    )
                )
        return self.review(start, end)

    def remove(
        self,
        start: date,
        end: date,
        need_id: UUID,
        product_id: str,
        *,
        channel: HistoryChannel = "api",
    ) -> BasketReview:
        self._week(start, end)
        selection_id = uuid5(
            NAMESPACE_URL, f"basket:{start}:{end}:{need_id}:oda:{product_id.strip()}"
        )
        with self.selection_store.session() as session:
            session.acquire_need_locks(start, end, [need_id])
            payload = session.get_selection_payload(start, end, selection_id)
            if payload is None:
                raise KeyError(f"basket selection not found: {product_id}")
            selection = _selection_adapter.validate_python(payload)
            session.record_history(
                HistoryEntryCreate(
                    channel=channel,
                    kind="domain_event",
                    message=f"Removed {selection.product.name} from the shopping basket",
                    structured={
                        "action": "basket_product_removed",
                        "week_start": start.isoformat(),
                        "week_end": end.isoformat(),
                        "need_id": str(need_id),
                        "provider": selection.product.provider,
                        "product_id": selection.product.product_id,
                        "packages": selection.packages,
                        "allocated_quantity": str(
                            selection.product.pack_quantity * selection.packages
                        ),
                    },
                )
            )
            session.delete_selection_by_id(selection_id)
        return self.review(start, end)

    def _week(self, start: date, end: date) -> WeekShopping:
        plan = self.purchase_plan.plan(start, end)
        return WeekShopping(
            lines=[
                ShoppingLine(
                    name=line.name,
                    quantity=line.to_buy_quantity,
                    unit=line.unit,
                    category=line.category,
                    origins=line.origins,
                    approximate=line.approximate,
                    provenance=line.provenance,
                    source_url=line.source_url,
                )
                for line in plan.purchase_lines
            ],
            suggestions=plan.suggestions,
            unquantified=plan.unquantified,
        )

    @staticmethod
    def _needs(week: WeekShopping, start: date, end: date) -> list[PurchaseNeed]:
        return [
            PurchaseNeed(
                id=need_id(start, end, line.name, line.unit),
                name=line.name,
                quantity=line.quantity,
                unit=line.unit,
                origins=line.origins,
                category=line.category,
                approximate=line.approximate,
                provenance=line.provenance,
                source_url=line.source_url,
            )
            for line in week.lines
        ]

    def _load(self, start: date, end: date, *, connection: Any = None) -> list[dict]:
        return self.selection_store.load_selections(start, end, connection=connection)
