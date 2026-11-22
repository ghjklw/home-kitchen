"""PostgreSQL and in-memory secondary adapters for Cookidoo My Week plan sync."""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.engine import Connection, Engine

from home_kitchen.history import HistoryEntryCreate, HistoryRepository
from home_kitchen.models import Meal, Recipe
from home_kitchen.recipes.adapters.postgres import source_links
from home_kitchen.recipes.ports import GenerationLockableProvider, HistoryRecordPort
from home_kitchen.store import entity_values, meals, recipes


class PostgresCookidooPlanSyncSession:
    """SQLAlchemy session for transactional Cookidoo plan sync writes and history."""

    def __init__(self, connection: Connection, history: HistoryRecordPort) -> None:
        self._connection = connection
        self._history = history

    def lock_provider_generation(
        self, provider: GenerationLockableProvider, actor_id: str, generation: object
    ) -> None:
        provider.lock_generation(self._connection, actor_id, generation)

    def add_meal(self, meal: Meal) -> None:
        self._connection.execute(insert(meals).values(**entity_values(meal)))

    def get_meal(self, meal_id: UUID) -> Meal | None:
        row = (
            self._connection.execute(select(meals).where(meals.c.id == meal_id)).mappings().first()
        )
        return Meal.model_validate(dict(row)) if row is not None else None

    def update_meal(self, meal: Meal) -> None:
        values = entity_values(meal)
        values.pop("id")
        values.pop("version", None)
        updated_id = self._connection.execute(
            update(meals)
            .where(meals.c.id == meal.id)
            .where(meals.c.version == meal.version)
            .values(**values, version=meals.c.version + 1)
            .returning(meals.c.id)
        ).scalar_one_or_none()
        if updated_id is None:
            raise ValueError("Måltidet ble endret før bekreftelse; forhåndsvis på nytt.")

    def delete_meal(self, meal_id: UUID) -> None:
        self._connection.execute(meals.delete().where(meals.c.id == meal_id))

    def find_sync_history(self, direction: str, external_entry_id: str) -> dict[str, Any] | None:
        if inspect(self._connection).has_table("kitchen_history"):
            row = (
                self._connection.execute(
                    text(
                        "SELECT structured FROM kitchen_history "
                        "WHERE kind = 'planning' "
                        "AND structured->>'provider' = 'cookidoo' "
                        "AND structured->>'direction' = :direction "
                        "AND structured->>'external_entry_id' = :external_entry_id "
                        "ORDER BY occurred_at DESC, id DESC LIMIT 1"
                    ),
                    {"direction": direction, "external_entry_id": external_entry_id},
                )
                .mappings()
                .first()
            )
            if row is not None:
                structured = row["structured"]
                if isinstance(structured, str):
                    structured = json.loads(structured)
                return dict(structured) if isinstance(structured, dict) else None

        entries = getattr(self._history, "entries", None)
        if isinstance(entries, list):
            for data, _actor in reversed(entries):
                structured = getattr(data, "structured", None)
                if (
                    getattr(data, "kind", None) == "planning"
                    and isinstance(structured, dict)
                    and structured.get("provider") == "cookidoo"
                    and structured.get("direction") == direction
                    and structured.get("external_entry_id") == external_entry_id
                ):
                    return dict(structured)
        return None

    def record_sync_history(
        self,
        *,
        actor_id: str,
        direction: str,
        action: str,
        meal_id: UUID | None,
        recipe_id: UUID | None,
        cookidoo_recipe_id: str,
        source_url: str,
        scheduled_date: date,
        message: str,
        external_entry_id: str | None = None,
        include_external_entry_id: bool = False,
        sync_state: dict[str, Any] | None = None,
    ) -> None:
        structured: dict[str, Any] = {
            "provider": "cookidoo",
            "direction": direction,
            "action": action,
            "meal_id": str(meal_id) if meal_id is not None else None,
            "cookidoo_recipe_id": cookidoo_recipe_id,
            "source_url": source_url,
            "date": scheduled_date.isoformat(),
        }
        if include_external_entry_id:
            structured["external_entry_id"] = external_entry_id
        if sync_state is not None:
            structured["sync_state"] = sync_state
        self._history.record(
            HistoryEntryCreate(
                kind="planning",
                message=message,
                recipe_id=recipe_id,
                structured=structured,
            ),
            actor_id=actor_id,
            connection=self._connection,
        )


class PostgresCookidooPlanSyncUnitOfWork:
    """PostgreSQL / SQLAlchemy unit of work for Cookidoo My Week plan sync."""

    def __init__(self, engine: Engine, history: HistoryRecordPort | None = None) -> None:
        self._engine = engine
        self.history: HistoryRecordPort = (
            history if history is not None else HistoryRepository(engine)
        )

    def load_kitchen_state(
        self, start: date, end: date
    ) -> tuple[
        list[Meal],
        dict[UUID, dict[str, Any]],
        dict[UUID, tuple[str, str]],
        dict[str, UUID],
    ]:
        with self._engine.connect() as conn:
            meal_rows = (
                conn.execute(
                    select(meals)
                    .where(meals.c.date >= start, meals.c.date <= end)
                    .order_by(meals.c.date, meals.c.position)
                )
                .mappings()
                .all()
            )
            kitchen_meals = [Meal.model_validate(dict(row)) for row in meal_rows]

            recipe_rows = conn.execute(select(recipes)).mappings().all()
            recipes_by_id: dict[UUID, dict[str, Any]] = {}
            for row in recipe_rows:
                rid = row["id"]
                rid_uuid = rid if isinstance(rid, UUID) else UUID(str(rid))
                recipes_by_id[rid_uuid] = dict(row)

            link_rows = (
                conn.execute(select(source_links).where(source_links.c.provider == "cookidoo"))
                .mappings()
                .all()
            )
            links_by_recipe: dict[UUID, tuple[str, str]] = {}
            links_by_ext_id: dict[str, UUID] = {}
            for row in link_rows:
                rid = row["recipe_id"]
                rid_uuid = rid if isinstance(rid, UUID) else UUID(str(rid))
                ext_id = str(row["external_recipe_id"])
                src_url = str(row["source_url"])
                links_by_recipe[rid_uuid] = (ext_id, src_url)
                links_by_ext_id[ext_id] = rid_uuid

        return kitchen_meals, recipes_by_id, links_by_recipe, links_by_ext_id

    def load_sync_history(self) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            if not inspect(conn).has_table("kitchen_history"):
                entries = getattr(self.history, "entries", None)
                if isinstance(entries, list):
                    return [
                        dict(data.structured)
                        for data, _actor in entries
                        if isinstance(getattr(data, "structured", None), dict)
                        and data.structured.get("provider") == "cookidoo"
                    ]
                return []
            rows = (
                conn.execute(
                    text(
                        "SELECT structured FROM kitchen_history "
                        "WHERE kind = 'planning' AND structured->>'provider' = 'cookidoo' "
                        "ORDER BY occurred_at, id"
                    )
                )
                .mappings()
                .all()
            )
        history: list[dict[str, Any]] = []
        for row in rows:
            structured = row["structured"]
            if isinstance(structured, str):
                structured = json.loads(structured)
            if isinstance(structured, dict):
                history.append(structured)
        return history

    @contextmanager
    def session(self) -> Generator[PostgresCookidooPlanSyncSession]:
        with self._engine.begin() as connection:
            yield PostgresCookidooPlanSyncSession(connection, self.history)


class MemoryCookidooPlanSyncSession:
    """In-memory transactional session for Cookidoo plan sync unit tests."""

    def __init__(
        self,
        meals_by_id: dict[UUID, Meal],
        history_entries: list[dict[str, Any]],
    ) -> None:
        self._meals = meals_by_id
        self._history_entries = history_entries

    def lock_provider_generation(
        self, provider: GenerationLockableProvider, actor_id: str, generation: object
    ) -> None:
        provider.lock_generation(None, actor_id, generation)

    def add_meal(self, meal: Meal) -> None:
        self._meals[meal.id] = meal.model_copy(deep=True)

    def get_meal(self, meal_id: UUID) -> Meal | None:
        found = self._meals.get(meal_id)
        return found.model_copy(deep=True) if found is not None else None

    def update_meal(self, meal: Meal) -> None:
        current = self._meals.get(meal.id)
        if current is None or current.version != meal.version:
            raise ValueError("Måltidet ble endret før bekreftelse; forhåndsvis på nytt.")
        self._meals[meal.id] = meal.model_copy(update={"version": current.version + 1}, deep=True)

    def delete_meal(self, meal_id: UUID) -> None:
        self._meals.pop(meal_id, None)

    def find_sync_history(self, direction: str, external_entry_id: str) -> dict[str, Any] | None:
        for entry in reversed(self._history_entries):
            structured = entry.get("structured") or {}
            if (
                entry.get("kind") == "planning"
                and structured.get("provider") == "cookidoo"
                and structured.get("direction") == direction
                and structured.get("external_entry_id") == external_entry_id
            ):
                return dict(structured)
        return None

    def record_sync_history(
        self,
        *,
        actor_id: str,
        direction: str,
        action: str,
        meal_id: UUID | None,
        recipe_id: UUID | None,
        cookidoo_recipe_id: str,
        source_url: str,
        scheduled_date: date,
        message: str,
        external_entry_id: str | None = None,
        include_external_entry_id: bool = False,
        sync_state: dict[str, Any] | None = None,
    ) -> None:
        structured: dict[str, Any] = {
            "provider": "cookidoo",
            "direction": direction,
            "action": action,
            "meal_id": str(meal_id) if meal_id is not None else None,
            "cookidoo_recipe_id": cookidoo_recipe_id,
            "source_url": source_url,
            "date": scheduled_date.isoformat(),
        }
        if include_external_entry_id:
            structured["external_entry_id"] = external_entry_id
        if sync_state is not None:
            structured["sync_state"] = sync_state
        self._history_entries.append(
            {
                "actor_id": actor_id,
                "kind": "planning",
                "message": message,
                "recipe_id": recipe_id,
                "structured": structured,
            }
        )


class MemoryCookidooPlanSyncUnitOfWork:
    """In-memory unit of work with copy-on-write rollback for Cookidoo plan sync tests."""

    def __init__(
        self,
        *,
        meals: dict[UUID, Meal] | None = None,
        recipes: dict[UUID, Recipe] | None = None,
        source_links: dict[tuple[str, str], dict[str, Any]] | None = None,
        collection_uow: Any = None,
    ) -> None:
        self.meals: dict[UUID, Meal] = dict(meals or {})
        self._recipes: dict[UUID, Recipe] = recipes if recipes is not None else {}
        self._source_links: dict[tuple[str, str], dict[str, Any]] = (
            source_links if source_links is not None else {}
        )
        self._collection_uow = collection_uow
        self.history_entries: list[dict[str, Any]] = []

    @property
    def recipes(self) -> dict[UUID, Recipe]:
        if self._collection_uow is not None:
            return self._collection_uow.recipes
        return self._recipes

    @property
    def source_links(self) -> dict[tuple[str, str], dict[str, Any]]:
        if self._collection_uow is not None:
            return self._collection_uow.source_links
        return self._source_links

    def load_kitchen_state(
        self, start: date, end: date
    ) -> tuple[
        list[Meal],
        dict[UUID, dict[str, Any]],
        dict[UUID, tuple[str, str]],
        dict[str, UUID],
    ]:
        kitchen_meals = sorted(
            (
                meal.model_copy(deep=True)
                for meal in self.meals.values()
                if start <= meal.date <= end
            ),
            key=lambda m: m.date,
        )
        recipes_by_id = {rid: recipe.model_dump() for rid, recipe in self.recipes.items()}
        links_by_recipe: dict[UUID, tuple[str, str]] = {}
        links_by_ext_id: dict[str, UUID] = {}
        for (provider, ext_id), link in self.source_links.items():
            if provider != "cookidoo":
                continue
            rid = link["recipe_id"]
            src_url = str(link.get("source_url") or "")
            links_by_recipe[rid] = (ext_id, src_url)
            links_by_ext_id[ext_id] = rid
        return kitchen_meals, recipes_by_id, links_by_recipe, links_by_ext_id

    def load_sync_history(self) -> list[dict[str, Any]]:
        return [
            dict(entry.get("structured") or {})
            for entry in self.history_entries
            if entry.get("kind") == "planning"
            and (entry.get("structured") or {}).get("provider") == "cookidoo"
        ]

    @contextmanager
    def session(self) -> Generator[MemoryCookidooPlanSyncSession]:
        meals_snapshot = {k: v.model_copy(deep=True) for k, v in self.meals.items()}
        history_snapshot = list(self.history_entries)
        try:
            yield MemoryCookidooPlanSyncSession(self.meals, self.history_entries)
        except Exception:
            self.meals = meals_snapshot
            self.history_entries = history_snapshot
            raise
