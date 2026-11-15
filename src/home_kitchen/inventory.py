"""Explicit stock counts offset requirements without consuming stock or converting units."""

import builtins
from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from home_kitchen.context import history_context
from home_kitchen.history import HistoryEntryCreate, HistoryRepository
from home_kitchen.models import NonBlank, RegularItem
from home_kitchen.service import KitchenService
from home_kitchen.shopping import need_id, normalize_need_text

StockQuantity = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class InventoryItemCreate(BaseModel):
    name: NonBlank
    unit: NonBlank = "item"
    quantity: StockQuantity
    category: NonBlank = "Other"
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("unit")
    @classmethod
    def normalized_unit(cls, value: str) -> str:
        return normalize_need_text(value)


class InventoryItem(InventoryItemCreate):
    id: UUID


class PurchasePlanLine(BaseModel):
    id: UUID
    name: str
    unit: str
    category: str = "Other"
    required_quantity: StockQuantity
    available_quantity: StockQuantity
    covered_quantity: StockQuantity
    to_buy_quantity: StockQuantity
    origins: list[str]


class PurchasePlan(BaseModel):
    start: date
    end: date
    lines: list[PurchasePlanLine]
    suggestions: list[RegularItem]

    @property
    def purchase_lines(self) -> list[PurchasePlanLine]:
        return [line for line in self.lines if line.to_buy_quantity > 0]

    @property
    def groups(self) -> dict[str, list[PurchasePlanLine]]:
        result: dict[str, list[PurchasePlanLine]] = {}
        for line in self.lines:
            result.setdefault(line.category, []).append(line)
        return result


class InventoryService:
    def __init__(self, kitchen: KitchenService, history: HistoryRepository | None = None):
        self.kitchen = kitchen
        self.engine = kitchen.store.engine
        self.history = history or HistoryRepository(self.engine)

    def list(self) -> builtins.list[InventoryItem]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, name, unit, quantity, category, note FROM kitchen_inventory "
                    "ORDER BY category, normalized_name, normalized_unit"
                )
            ).mappings()
            return [InventoryItem.model_validate(dict(row)) for row in rows]

    def upsert(self, data: InventoryItemCreate) -> InventoryItem:
        """Set an absolute quantity; repeating a count never adds stock twice."""
        name_key = normalize_need_text(data.name)
        unit_key = normalize_need_text(data.unit)
        item = InventoryItem(
            id=uuid5(NAMESPACE_URL, f"kitchen:inventory:{name_key}:{unit_key}"),
            **data.model_dump(),
        )
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                        "INSERT INTO kitchen_inventory "
                        "(id,name,normalized_name,unit,normalized_unit,quantity,category,note) "
                        "VALUES (:id,:name,:normalized_name,:unit,:normalized_unit,:quantity,:category,:note) "
                        "ON CONFLICT (normalized_name,normalized_unit) DO UPDATE SET "
                        "name=EXCLUDED.name,unit=EXCLUDED.unit,quantity=EXCLUDED.quantity,"
                        "category=EXCLUDED.category,note=EXCLUDED.note "
                        "RETURNING id,name,unit,quantity,category,note"
                    ),
                    {**item.model_dump(), "normalized_name": name_key, "normalized_unit": unit_key},
                )
                .mappings()
                .one()
            )
            saved = InventoryItem.model_validate(dict(row))
            self.history.record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="domain_event",
                    message=f"Updated available stock of {saved.name}",
                    structured={"action": "inventory_count_set", **saved.model_dump(mode="json")},
                ),
                connection=connection,
            )
        return saved

    def remove(self, item_id: UUID) -> None:
        with self.engine.begin() as connection:
            row = connection.execute(
                text("DELETE FROM kitchen_inventory WHERE id=:id RETURNING name"), {"id": item_id}
            ).first()
            if row is None:
                raise KeyError(str(item_id))
            self.history.record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="domain_event",
                    message=f"Removed {row[0]} from available stock",
                    structured={"action": "inventory_removed", "id": str(item_id)},
                ),
                connection=connection,
            )

    def plan(self, start: date, end: date) -> PurchasePlan:
        week = self.kitchen.shopping_list(start, end, today=date.today())
        stock = {
            (normalize_need_text(item.name), normalize_need_text(item.unit)): item
            for item in self.list()
        }
        lines = []
        for requirement in week.lines:
            item = stock.get(
                (normalize_need_text(requirement.name), normalize_need_text(requirement.unit))
            )
            available = item.quantity if item else Decimal(0)
            covered = min(requirement.quantity, available)
            category = requirement.category
            if category == "Other" and item:
                category = item.category
            lines.append(
                PurchasePlanLine(
                    id=need_id(start, end, requirement.name, requirement.unit),
                    name=requirement.name,
                    unit=requirement.unit,
                    category=category,
                    required_quantity=requirement.quantity,
                    available_quantity=available,
                    covered_quantity=covered,
                    to_buy_quantity=requirement.quantity - covered,
                    origins=requirement.origins,
                )
            )
        return PurchasePlan(start=start, end=end, lines=lines, suggestions=week.suggestions)
