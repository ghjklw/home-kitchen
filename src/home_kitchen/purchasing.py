"""Purchase modes share a plan; in-store progress never asserts a paid purchase."""

from datetime import date
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, Field

from home_kitchen.context import history_context
from home_kitchen.history import HistoryEntryCreate
from home_kitchen.models import UnquantifiedNeed
from home_kitchen.purchasing_ports import (
    HistoryRecordPort,
    OdaStatusPort,
    PurchasePlanPort,
    StoreTripRepository,
)
from home_kitchen.shopping import PurchasePlanLine


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
    def __init__(self, service: OdaStatusPort):
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
    unquantified: list[UnquantifiedNeed] = Field(default_factory=list)
    start: date
    end: date
    lines: list[StoreTripLine]
    checked_count: int
    total_count: int


class StoreTripService:
    def __init__(
        self,
        inventory: PurchasePlanPort,
        history: HistoryRecordPort | None = None,
        *,
        repository: StoreTripRepository | None = None,
        engine: Any = None,
    ):
        self.inventory = inventory
        self.history = history
        self._repository = repository
        self._engine = (
            engine
            if engine is not None
            else getattr(repository, "engine", None) or getattr(history, "engine", None)
        )

    @property
    def engine(self) -> Any:
        return self._engine

    @engine.setter
    def engine(self, value: Any) -> None:
        self._engine = value
        if self._repository is not None and hasattr(self._repository, "engine"):
            cast(Any, self._repository).engine = value

    def _resolve_repository(self) -> StoreTripRepository:
        if self._repository is None:
            from home_kitchen.purchasing_adapters import PostgresStoreTripRepository

            self._repository = PostgresStoreTripRepository(self._engine, self.history)
        return self._repository

    def review(self, start: date, end: date) -> StoreTrip:
        plan = self.inventory.plan(start, end)
        progress = self._resolve_repository().load_checked_quantities(start, end)
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
            unquantified=plan.unquantified,
        )

    def check(self, start: date, end: date, need_id: UUID, checked: bool) -> StoreTrip:
        line = next((line for line in self.review(start, end).lines if line.id == need_id), None)
        if line is None:
            raise ValueError("This item no longer needs buying; refresh the shopping plan")
        quantity = line.to_buy_quantity if checked else Decimal(0)
        entry = HistoryEntryCreate(
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
        )
        self._resolve_repository().save_check(
            start=start,
            end=end,
            need_id=need_id,
            quantity=quantity,
            history_entry=entry,
        )
        return self.review(start, end)
