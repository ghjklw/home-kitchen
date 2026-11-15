"""Persistence and validation for a reviewed, non-checkout shopping basket."""

import json
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.engine import Engine

from home_kitchen.history import HistoryChannel, HistoryEntryCreate, HistoryRepository
from home_kitchen.inventory import InventoryService
from home_kitchen.models import ShoppingLine, WeekShopping
from home_kitchen.service import KitchenService
from home_kitchen.shopping import need_id

from .evaluation import evaluate_basket
from .models import BasketReview, PurchaseNeed, SelectedProduct

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

    def __init__(self, kitchen: KitchenService, history: HistoryRepository):
        self.kitchen = kitchen
        self.engine: Engine = kitchen.store.engine
        self.history = history
        self.inventory = InventoryService(kitchen, history)

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
        if selection.product.pack_unit != need.unit:
            raise ValueError(
                f"incompatible units for {selection.product.name}: "
                f"{selection.product.pack_unit} cannot satisfy {need.unit}"
            )

        existing_rows = self._load(start, end)
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

        now = datetime.now(UTC)
        selection_id = _selection_id(start, end, selection)
        payload = selection.model_dump(mode="json")
        allocated = selection.product.pack_quantity * selection.packages
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
                "need_quantity": str(need.quantity),
            },
        )
        with self.engine.begin() as connection:
            connection.execute(
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
                    "need_id": selection.need_id,
                    "provider": selection.product.provider,
                    "product_id": selection.product.product_id,
                    "payload": json.dumps(payload),
                    "updated_at": now,
                },
            )
            self.history.record(event, connection=connection)
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
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT payload FROM kitchen_basket_selections WHERE id = :id "
                        "AND week_start = :week_start AND week_end = :week_end"
                    ),
                    {"id": selection_id, "week_start": start, "week_end": end},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise KeyError(f"basket selection not found: {product_id}")
            selection = _selection_adapter.validate_python(row["payload"])
            self.history.record(
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
                ),
                connection=connection,
            )
            connection.execute(
                text("DELETE FROM kitchen_basket_selections WHERE id = :id"),
                {"id": selection_id},
            )
        return self.review(start, end)

    def _week(self, start: date, end: date) -> WeekShopping:
        plan = self.inventory.plan(start, end)
        return WeekShopping(
            lines=[
                ShoppingLine(
                    name=line.name,
                    quantity=line.to_buy_quantity,
                    unit=line.unit,
                    category=line.category,
                    origins=line.origins,
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
            )
            for line in week.lines
        ]

    def _load(self, start: date, end: date) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, need_id, provider, product_id, payload "
                    "FROM kitchen_basket_selections "
                    "WHERE week_start = :week_start AND week_end = :week_end"
                ),
                {"week_start": start, "week_end": end},
            ).mappings()
            return [dict(row) for row in rows]
