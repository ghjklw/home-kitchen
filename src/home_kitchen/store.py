"""Small SQLite entity store; every operation owns its connection."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel

from .models import Meal, Recipe, RegularItem, ShoppingItem

Model = TypeVar("Model", bound=BaseModel)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    meal_date TEXT,
                    meal_slot TEXT,
                    PRIMARY KEY (kind, id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS meal_slots
                    ON entities (meal_date, meal_slot) WHERE kind = 'meal';
                CREATE TABLE IF NOT EXISTS confirmations (
                    regular_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, kind: str, entity: Recipe | Meal | RegularItem | ShoppingItem) -> None:
        meal_date = entity.date.isoformat() if isinstance(entity, Meal) else None
        meal_slot = entity.slot if isinstance(entity, Meal) else None
        with self.connection() as connection:
            if kind == "meal":
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM entities WHERE kind = 'meal' AND meal_date = ? AND meal_slot = ?",
                    (meal_date, meal_slot),
                )
            connection.execute(
                """INSERT INTO entities (kind, id, payload, meal_date, meal_slot)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(kind, id) DO UPDATE SET payload = excluded.payload,
                   meal_date = excluded.meal_date, meal_slot = excluded.meal_slot""",
                (
                    kind,
                    str(entity.id),
                    entity.model_dump_json(),
                    meal_date,
                    meal_slot,
                ),
            )

    def get(self, kind: str, entity_id: UUID, model: type[Model]) -> Model:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT payload FROM entities WHERE kind = ? AND id = ?",
                (kind, str(entity_id)),
            ).fetchone()
        if row is None:
            raise KeyError(str(entity_id))
        return model.model_validate_json(row[0])

    def list(self, kind: str, model: type[Model]) -> list[Model]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM entities WHERE kind = ? ORDER BY rowid", (kind,)
            ).fetchall()
        return [model.model_validate_json(row[0]) for row in rows]

    def delete(self, kind: str, entity_id: UUID) -> None:
        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM entities WHERE kind = ? AND id = ?", (kind, str(entity_id))
            )
            if cursor.rowcount == 0:
                raise KeyError(str(entity_id))

    def active_confirmations(self) -> set[UUID]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT c.regular_id, e.payload FROM confirmations c JOIN entities e
                   ON e.kind = 'item' AND e.id = c.item_id"""
            ).fetchall()
        return {
            UUID(regular_id)
            for regular_id, payload in rows
            if not ShoppingItem.model_validate_json(payload).completed
        }

    def confirm(self, regular_id: UUID, item: ShoppingItem) -> ShoppingItem:
        """Atomically reuse the current unfinished capture, even across processes."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            regular = connection.execute(
                "SELECT payload FROM entities WHERE kind = 'regular' AND id = ?",
                (str(regular_id),),
            ).fetchone()
            if regular is None:
                raise KeyError(str(regular_id))
            RegularItem.model_validate_json(regular[0])
            row = connection.execute(
                """SELECT e.payload FROM confirmations c JOIN entities e
                   ON e.kind = 'item' AND e.id = c.item_id WHERE c.regular_id = ?""",
                (str(regular_id),),
            ).fetchone()
            if row is not None:
                existing = ShoppingItem.model_validate_json(row[0])
                if not existing.completed:
                    return existing
            connection.execute(
                "INSERT INTO entities (kind, id, payload) VALUES ('item', ?, ?)",
                (str(item.id), item.model_dump_json()),
            )
            connection.execute(
                """INSERT INTO confirmations (regular_id, item_id) VALUES (?, ?)
                   ON CONFLICT(regular_id) DO UPDATE SET item_id = excluded.item_id""",
                (str(regular_id), str(item.id)),
            )
        return item
