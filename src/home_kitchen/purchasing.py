"""Purchase modes share a plan; in-store progress never asserts a paid purchase."""

from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import text

from home_kitchen.context import history_context
from home_kitchen.history import HistoryEntryCreate, HistoryRepository
from home_kitchen.inventory import InventoryService, PurchasePlanLine
from home_kitchen.oda import OdaService


class PurchaseModeInfo(BaseModel):
    id: str
    name: str
    available: bool
    detail: str


class PurchaseMode(Protocol):
    def info(self) -> PurchaseModeInfo: ...


class InStoreMode:
    def info(self) -> PurchaseModeInfo:
        return PurchaseModeInfo(
            id="in_store",
            name="In store",
            available=True,
            detail="Group what still needs buying by category and check items as you pick them up.",
        )


class OdaMode:
    def __init__(self, service: OdaService):
        self.service = service

    def info(self) -> PurchaseModeInfo:
        connected = self.service.status(history_context.get().actor_id or "")["connected"]
        return PurchaseModeInfo(
            id="oda",
            name="Oda",
            available=connected,
            detail="Connected to Oda for product search and cart review. Pay at Oda."
            if connected
            else "Connect your Oda account directly in Kitchen. Review and pay at Oda.",
        )


class PurchaseModes:
    def __init__(self, modes: list[PurchaseMode]):
        self._modes: dict[str, PurchaseMode] = {}
        for mode in modes:
            self.register(mode)

    def register(self, mode: PurchaseMode) -> None:
        mode_id = mode.info().id
        if mode_id in self._modes:
            raise ValueError("Purchase mode already registered")
        self._modes[mode_id] = mode

    def catalog(self) -> list[PurchaseModeInfo]:
        return [mode.info() for mode in self._modes.values()]


class StoreTripLine(PurchasePlanLine):
    checked: bool = False
    checked_quantity: Decimal = Decimal(0)


class StoreTrip(BaseModel):
    start: date
    end: date
    lines: list[StoreTripLine]
    checked_count: int
    total_count: int


class StoreTripService:
    def __init__(self, inventory: InventoryService, history: HistoryRepository):
        self.inventory = inventory
        self.history = history
        self.engine = history.engine

    def review(self, start: date, end: date) -> StoreTrip:
        plan = self.inventory.plan(start, end)
        with self.engine.connect() as connection:
            progress = dict(
                connection.execute(
                    text(
                        "SELECT need_id, checked_quantity FROM kitchen_store_trip "
                        "WHERE week_start = :start AND week_end = :end"
                    ),
                    {"start": start, "end": end},
                ).all()
            )
        lines = []
        for line in plan.lines:
            if line.to_buy_quantity <= 0:
                continue
            quantity = progress.get(line.id, Decimal(0))
            lines.append(
                StoreTripLine(
                    **line.model_dump(),
                    checked=quantity >= line.to_buy_quantity,
                    checked_quantity=quantity,
                )
            )
        lines.sort(key=lambda line: (line.category.casefold(), line.name.casefold(), line.unit))
        return StoreTrip(
            start=start,
            end=end,
            lines=lines,
            checked_count=sum(line.checked for line in lines),
            total_count=len(lines),
        )

    def check(self, start: date, end: date, need_id: UUID, checked: bool) -> StoreTrip:
        line = next((line for line in self.review(start, end).lines if line.id == need_id), None)
        if line is None:
            raise ValueError("This item no longer needs buying; refresh the shopping plan")
        quantity = line.to_buy_quantity if checked else Decimal(0)
        with self.engine.begin() as connection:
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
            if changed is not None:
                self.history.record(
                    HistoryEntryCreate(
                        channel=history_context.get().channel,
                        kind="domain_event",
                        message=f"{'Picked up' if checked else 'Unchecked'} {line.name} in store",
                        structured={
                            "action": "store_trip_checkoff",
                            "need_id": str(need_id),
                            "checked": checked,
                            "quantity": str(quantity),
                            "unit": line.unit,
                            "week_start": start.isoformat(),
                            "week_end": end.isoformat(),
                        },
                    ),
                    connection=connection,
                )
        return self.review(start, end)
