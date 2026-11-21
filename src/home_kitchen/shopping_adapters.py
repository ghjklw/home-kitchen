"""PostgreSQL and in-memory adapters for ShoppingItemRepository."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from home_kitchen.context import history_context
from home_kitchen.history import HistoryEntryCreate, HistoryRepository
from home_kitchen.models import RegularItem, ShoppingItem
from home_kitchen.purchasing_ports import HistoryRecordPort
from home_kitchen.shopping import ShoppingItemRepository
from home_kitchen.store import confirmations, entity_values, items, regulars


class PostgresShoppingItemRepository(ShoppingItemRepository):
    """PostgreSQL adapter persisting capture items, regular items, and suggestion confirmations."""

    def __init__(self, engine: Engine, history: HistoryRecordPort | None = None) -> None:
        self._engine = engine
        self._history: HistoryRecordPort = (
            history if history is not None else HistoryRepository(engine)
        )

    def save_item(self, item: ShoppingItem) -> None:
        values = entity_values(item)
        statement = pg_insert(items).values(**values)
        with self._engine.begin() as connection:
            connection.execute(
                statement.on_conflict_do_update(index_elements=[items.c.id], set_=values)
            )
            self._history.record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="domain_event",
                    message="Saved item",
                    structured={
                        "entity_type": "item",
                        "entity_id": str(item.id),
                        "data": item.model_dump(mode="json"),
                    },
                ),
                connection=connection,
            )

    def get_item(self, item_id: UUID) -> ShoppingItem:
        with self._engine.begin() as connection:
            row = connection.execute(select(items).where(items.c.id == item_id)).mappings().first()
        if row is None:
            raise KeyError(str(item_id))
        return ShoppingItem.model_validate(dict(row))

    def list_items(self) -> list[ShoppingItem]:
        with self._engine.begin() as connection:
            rows = connection.execute(select(items).order_by(items.c.position)).mappings().all()
        return [ShoppingItem.model_validate(dict(row)) for row in rows]

    def delete_item(self, item_id: UUID) -> None:
        with self._engine.begin() as connection:
            result = connection.execute(items.delete().where(items.c.id == item_id))
            if result.rowcount == 0:
                raise KeyError(str(item_id))

    def save_regular(self, item: RegularItem) -> None:
        values = entity_values(item)
        statement = pg_insert(regulars).values(**values)
        with self._engine.begin() as connection:
            previous = connection.execute(
                select(regulars.c.last_purchased).where(regulars.c.id == item.id)
            ).first()
            connection.execute(
                statement.on_conflict_do_update(index_elements=[regulars.c.id], set_=values)
            )
            purchased = item.last_purchased is not None and (
                previous is None or previous[0] != item.last_purchased
            )
            self._history.record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="purchase" if purchased else "domain_event",
                    message=f"Purchased {item.name}" if purchased else "Saved regular",
                    structured={
                        "entity_type": "regular",
                        "entity_id": str(item.id),
                        "data": item.model_dump(mode="json"),
                    },
                ),
                connection=connection,
            )

    def get_regular(self, item_id: UUID) -> RegularItem:
        with self._engine.begin() as connection:
            row = (
                connection.execute(select(regulars).where(regulars.c.id == item_id))
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(str(item_id))
        return RegularItem.model_validate(dict(row))

    def list_regulars(self) -> list[RegularItem]:
        with self._engine.begin() as connection:
            rows = (
                connection.execute(select(regulars).order_by(regulars.c.position)).mappings().all()
            )
        return [RegularItem.model_validate(dict(row)) for row in rows]

    def active_confirmations(self) -> set[UUID]:
        with self._engine.begin() as connection:
            return set(
                connection.execute(
                    select(confirmations.c.regular_id)
                    .join(items)
                    .where(items.c.completed.is_(False))
                ).scalars()
            )

    def confirm_regular(self, regular_id: UUID, item: ShoppingItem) -> ShoppingItem:
        """Lock the regular record so concurrent requests reuse its active capture."""
        with self._engine.begin() as connection:
            regular = connection.execute(
                select(regulars.c.id).where(regulars.c.id == regular_id).with_for_update()
            ).first()
            if regular is None:
                raise KeyError(str(regular_id))
            row = (
                connection.execute(
                    select(items)
                    .join(confirmations)
                    .where(confirmations.c.regular_id == regular_id)
                )
                .mappings()
                .first()
            )
            if row is not None and not row["completed"]:
                return ShoppingItem.model_validate(dict(row))
            connection.execute(pg_insert(items).values(**entity_values(item)))
            statement = pg_insert(confirmations).values(regular_id=regular_id, item_id=item.id)
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[confirmations.c.regular_id], set_={"item_id": item.id}
                )
            )
        return item


class MemoryShoppingItemRepository(ShoppingItemRepository):
    """Pure in-memory implementation of ShoppingItemRepository for unit testing."""

    def __init__(self) -> None:
        self.items: dict[UUID, ShoppingItem] = {}
        self.regulars: dict[UUID, RegularItem] = {}
        self.confirmations: dict[UUID, UUID] = {}
        self.history_entries: list[dict[str, Any]] = []

    def save_item(self, item: ShoppingItem) -> None:
        self.items[item.id] = item.model_copy(deep=True)
        self.history_entries.append(
            {"kind": "domain_event", "entity_type": "item", "entity_id": str(item.id)}
        )

    def get_item(self, item_id: UUID) -> ShoppingItem:
        if item_id not in self.items:
            raise KeyError(str(item_id))
        return self.items[item_id].model_copy(deep=True)

    def list_items(self) -> list[ShoppingItem]:
        return [item.model_copy(deep=True) for item in self.items.values()]

    def delete_item(self, item_id: UUID) -> None:
        if item_id not in self.items:
            raise KeyError(str(item_id))
        del self.items[item_id]

    def save_regular(self, item: RegularItem) -> None:
        previous = self.regulars.get(item.id)
        purchased = item.last_purchased is not None and (
            previous is None or previous.last_purchased != item.last_purchased
        )
        self.regulars[item.id] = item.model_copy(deep=True)
        self.history_entries.append(
            {
                "kind": "purchase" if purchased else "domain_event",
                "entity_type": "regular",
                "entity_id": str(item.id),
            }
        )

    def get_regular(self, item_id: UUID) -> RegularItem:
        if item_id not in self.regulars:
            raise KeyError(str(item_id))
        return self.regulars[item_id].model_copy(deep=True)

    def list_regulars(self) -> list[RegularItem]:
        return [item.model_copy(deep=True) for item in self.regulars.values()]

    def active_confirmations(self) -> set[UUID]:
        active: set[UUID] = set()
        for regular_id, item_id in self.confirmations.items():
            item = self.items.get(item_id)
            if item is not None and not item.completed:
                active.add(regular_id)
        return active

    def confirm_regular(self, regular_id: UUID, item: ShoppingItem) -> ShoppingItem:
        if regular_id not in self.regulars:
            raise KeyError(str(regular_id))
        existing_item_id = self.confirmations.get(regular_id)
        if existing_item_id is not None:
            existing = self.items.get(existing_item_id)
            if existing is not None and not existing.completed:
                return existing.model_copy(deep=True)
        self.items[item.id] = item.model_copy(deep=True)
        self.confirmations[regular_id] = item.id
        return item.model_copy(deep=True)
