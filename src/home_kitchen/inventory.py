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
from home_kitchen.models import NonBlank, RegularItem, UnquantifiedNeed
from home_kitchen.service import KitchenService
from home_kitchen.shopping import need_id, normalize_need_text
from home_kitchen.units import add_exact, canonicalize_quantity, compatible_units, normalize_unit

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
        return normalize_unit(value)


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
    unquantified: list[UnquantifiedNeed] = Field(default_factory=list)

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

            # Older rows may have used any supported display unit. Present their
            # exact total in the base unit while retaining one persisted row ID.
            grouped: dict[tuple[str, str], tuple[InventoryItem, bool]] = {}
            for row in rows:
                quantity, unit = canonicalize_quantity(Decimal(row["quantity"]), row["unit"])
                key = (normalize_need_text(row["name"]), unit)
                candidate = InventoryItem(
                    id=row["id"],
                    name=row["name"],
                    unit=unit,
                    quantity=quantity,
                    category=row["category"],
                    note=row["note"],
                )
                candidate_is_canonical = normalize_unit(row["unit"]) == unit
                current = grouped.get(key)
                if current is None:
                    grouped[key] = (candidate, candidate_is_canonical)
                    continue
                current_item, current_is_canonical = current
                total = add_exact(current_item.quantity, candidate.quantity)
                if candidate_is_canonical and not current_is_canonical:
                    grouped[key] = (
                        candidate.model_copy(update={"quantity": total}),
                        candidate_is_canonical,
                    )
                elif candidate_is_canonical == current_is_canonical and str(candidate.id) < str(
                    current_item.id
                ):
                    grouped[key] = (
                        candidate.model_copy(update={"quantity": total}),
                        candidate_is_canonical,
                    )
                else:
                    grouped[key] = (
                        current_item.model_copy(update={"quantity": total}),
                        current_is_canonical,
                    )
            return [item for item, _ in grouped.values()]

    def upsert(self, data: InventoryItemCreate) -> InventoryItem:
        """Set an absolute quantity; repeating a count never adds stock twice."""
        name_key = normalize_need_text(data.name)
        quantity, unit_key = canonicalize_quantity(data.quantity, data.unit)
        item = InventoryItem(
            id=uuid5(NAMESPACE_URL, f"kitchen:inventory:{name_key}:{unit_key}"),
            **data.model_dump(exclude={"quantity", "unit"}),
            quantity=quantity,
            unit=unit_key,
        )
        with self.engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"kitchen:stock:{name_key}:{unit_key}"},
            )
            legacy_keys = sorted(compatible_units(unit_key))
            unit_params = {f"unit_{index}": value for index, value in enumerate(legacy_keys)}
            placeholders = ",".join(f":{name}" for name in unit_params)
            rows = (
                connection.execute(
                    text(
                        "SELECT id, normalized_unit FROM kitchen_inventory "
                        f"WHERE normalized_name=:normalized_name AND normalized_unit IN ({placeholders}) "
                        "ORDER BY (normalized_unit=:canonical_unit) DESC, id FOR UPDATE"
                    ),
                    {
                        "normalized_name": name_key,
                        "canonical_unit": unit_key,
                        **unit_params,
                    },
                )
                .mappings()
                .all()
            )
            if rows:
                # Preserve the selected stock row's ID while moving it to the
                # canonical unit. Old duplicate unit rows remain as zero counts,
                # retaining their identities for clients that already saw them.
                item.id = rows[0]["id"]
                for row in rows[1:]:
                    connection.execute(
                        text("UPDATE kitchen_inventory SET quantity=0 WHERE id=:id"),
                        {"id": row["id"]},
                    )
                connection.execute(
                    text(
                        "UPDATE kitchen_inventory SET name=:name,normalized_name=:normalized_name,"
                        "unit=:unit,normalized_unit=:normalized_unit,quantity=:quantity,"
                        "category=:category,note=:note WHERE id=:id"
                    ),
                    {**item.model_dump(), "normalized_name": name_key, "normalized_unit": unit_key},
                )
                saved = item
            else:
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
                        {
                            **item.model_dump(),
                            "normalized_name": name_key,
                            "normalized_unit": unit_key,
                        },
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
            row = (
                connection.execute(
                    text("SELECT name,normalized_name,unit FROM kitchen_inventory WHERE id=:id"),
                    {"id": item_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise KeyError(str(item_id))
            _, unit_key = canonicalize_quantity(Decimal(0), row["unit"])
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"kitchen:stock:{row['normalized_name']}:{unit_key}"},
            )
            if (
                connection.execute(
                    text("SELECT id FROM kitchen_inventory WHERE id=:id"), {"id": item_id}
                ).first()
                is None
            ):
                raise KeyError(str(item_id))
            # A displayed stock record may aggregate compatible legacy rows.
            # Removing that record must remove its complete visible quantity.
            units = sorted(compatible_units(row["unit"]))
            params = {f"unit_{index}": unit for index, unit in enumerate(units)}
            placeholders = ",".join(f":{key}" for key in params)
            connection.execute(
                text(
                    "DELETE FROM kitchen_inventory WHERE normalized_name=:name "
                    f"AND normalized_unit IN ({placeholders})"
                ),
                {"name": row["normalized_name"], **params},
            )
            self.history.record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="domain_event",
                    message=f"Removed {row['name']} from available stock",
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
                    to_buy_quantity=add_exact(requirement.quantity, -covered),
                    origins=requirement.origins,
                )
            )
        return PurchasePlan(
            start=start,
            end=end,
            lines=lines,
            suggestions=week.suggestions,
            unquantified=week.unquantified,
        )
