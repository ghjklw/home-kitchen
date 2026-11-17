"""Tests for Option B recipe activity stats and 1-click quick feedback."""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from home_kitchen.app import create_app
from home_kitchen.basket.repository import BasketService
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.features import KitchenFeatures
from home_kitchen.history import HistoryEntry, HistoryEntryCreate
from home_kitchen.mcp_server import build_mcp
from home_kitchen.models import Ingredient, MealCreate, Recipe, RecipeActivityStats, RecipeCreate
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings
from home_kitchen.store import Store, entity_values


class _FakeMappingsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, meal_rows, history_rows, recipe_rows):
        self._meal_rows = meal_rows
        self._history_rows = history_rows
        self._recipe_rows = recipe_rows
        self.queries: list[tuple[str, dict | None]] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.queries.append((sql, params))
        if "FROM meals" in sql:
            return _FakeMappingsResult(self._meal_rows)
        if "FROM kitchen_history" in sql:
            return _FakeMappingsResult(self._history_rows)
        if "FROM recipes" in sql:
            return _FakeMappingsResult([(row["id"],) for row in self._recipe_rows])
        raise AssertionError(f"Unexpected SQL in test: {sql}")


class _FakeStatsStore(Store):
    def __init__(self, meal_rows=None, history_rows=None, recipe_rows=None):
        self.engine = None
        self._meal_rows = meal_rows or []
        self._history_rows = history_rows or []
        self._recipe_rows = recipe_rows or []
        self.last_connection: _FakeConnection | None = None

    @contextmanager
    def connection(self) -> Generator[_FakeConnection, None, None]:
        conn = _FakeConnection(self._meal_rows, self._history_rows, self._recipe_rows)
        self.last_connection = conn
        yield conn


class _StubEngine:
    def dispose(self) -> None:
        pass


class _InMemoryStore:
    """Minimal in-memory store for non-DB FastAPI and MCP contract tests."""

    def __init__(self):
        self.engine = _StubEngine()
        self.recipes: dict[UUID, Recipe] = {}
        self.planned_counts: dict[UUID, int] = {}
        self.cooked_counts: dict[UUID, int] = {}
        self.ordered_counts: dict[UUID, int] = {}
        self.feedback_entries: list[HistoryEntry] = []
        self.settings: dict[str, str] = {}

    def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def ensure_setting(self, key: str, value: str) -> str:
        return self.settings.setdefault(key, value)

    def save(self, kind: str, entity) -> None:
        if kind == "recipe":
            self.recipes[entity.id] = entity.model_copy(
                update={"activity_stats": None, "stats": None}
            )

    def get(self, kind: str, entity_id: UUID, model):
        if kind == "recipe":
            if entity_id not in self.recipes:
                raise KeyError(str(entity_id))
            return self.recipes[entity_id].model_copy(deep=True)
        raise KeyError(str(entity_id))

    def list(self, kind: str, model):
        if kind == "recipe":
            return [recipe.model_copy(deep=True) for recipe in self.recipes.values()]
        return []

    def get_recipe_activity_stats(self, recipe_id: UUID) -> RecipeActivityStats:
        return self.get_recipes_activity_stats([recipe_id]).get(recipe_id, RecipeActivityStats())

    def get_recipes_activity_stats(
        self, recipe_ids: list[UUID] | None = None
    ) -> dict[UUID, RecipeActivityStats]:
        target_ids = list(self.recipes) if recipe_ids is None else list(dict.fromkeys(recipe_ids))
        result: dict[UUID, RecipeActivityStats] = {}
        for recipe_id in target_ids:
            entries = [
                entry
                for entry in reversed(self.feedback_entries)
                if entry.recipe_id == recipe_id and entry.kind == "recipe_rating"
            ]
            ratings = [
                entry.structured["rating"]
                for entry in entries
                if isinstance(entry.structured.get("rating"), int)
                and not isinstance(entry.structured.get("rating"), bool)
            ]
            would_make = next(
                (
                    entry.structured["would_make_again"]
                    for entry in entries
                    if isinstance(entry.structured.get("would_make_again"), bool)
                ),
                None,
            )
            result[recipe_id] = RecipeActivityStats(
                times_planned=self.planned_counts.get(recipe_id, 0),
                times_cooked=self.cooked_counts.get(recipe_id, 0),
                times_ordered=self.ordered_counts.get(recipe_id, 0),
                latest_rating=ratings[0] if ratings else None,
                average_rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
                would_make_again=would_make,
                feedback_count=len(entries),
            )
        return result


class _InMemoryHistory:
    def __init__(self, store: _InMemoryStore):
        self.store = store

    def record(
        self,
        data: HistoryEntryCreate,
        actor_id: str | None = None,
        connection=None,
    ) -> HistoryEntry:
        entry = HistoryEntry(
            id=uuid4(),
            occurred_at=datetime.now(UTC),
            actor_id=actor_id,
            **data.model_dump(),
        )
        self.store.feedback_entries.append(entry)
        return entry


def _sample_recipe(title: str = "Fiskegrateng") -> Recipe:
    return Recipe(
        id=uuid4(),
        title=title,
        servings=4,
        ingredients=[Ingredient(name="Torsk", quantity=400, unit="g")],
        instructions=["Stek i ovnen."],
    )


def _make_memory_service():
    store = _InMemoryStore()
    service = KitchenService(store=store)  # type: ignore[arg-type]
    return service, store


def test_recipe_activity_stats_model_and_entity_values_strip():
    stats = RecipeActivityStats(
        times_planned=3,
        times_cooked=2,
        times_ordered=1,
        latest_rating=5,
        average_rating=4.5,
        would_make_again=True,
        feedback_count=2,
    )
    recipe_a = Recipe(
        id=uuid4(),
        title="Suppe",
        servings=2,
        ingredients=[Ingredient(name="Tomat", quantity=3, unit="item")],
        instructions=["Kok"],
        activity_stats=stats,
    )
    assert recipe_a.stats == stats

    recipe_b = Recipe(
        id=recipe_a.id,
        title="Suppe",
        servings=2,
        ingredients=[Ingredient(name="Tomat", quantity=3, unit="item")],
        instructions=["Kok"],
        stats=stats,
    )
    assert recipe_b.activity_stats == stats

    persisted = entity_values(recipe_a)
    assert "activity_stats" not in persisted
    assert "stats" not in persisted


def test_store_derives_stats_without_inferring_cooking_from_planning():
    r1 = uuid4()
    r2 = uuid4()
    r_unused = uuid4()
    other = uuid4()

    meal_rows = [
        {"recipe_id": r1, "count": 4},
        {"recipe_id": str(r2), "count": 1},
    ]
    # Ordered DESC by occurred_at, id as SQL query specifies
    history_rows = [
        # Most recent feedback on r1: 1-click thumbs up only
        {
            "id": uuid4(),
            "occurred_at": datetime(2026, 11, 16, 18, 0, tzinfo=UTC),
            "kind": "recipe_rating",
            "recipe_id": r1,
            "structured": {"would_make_again": True},
        },
        # Earlier feedback on r1: 1-click 5 stars
        {
            "id": uuid4(),
            "occurred_at": datetime(2026, 11, 15, 18, 0, tzinfo=UTC),
            "kind": "recipe_rating",
            "recipe_id": r1,
            "structured": {"rating": 5, "would_make_again": False},
        },
        # Earlier feedback on r1: 4 stars
        {
            "id": uuid4(),
            "occurred_at": datetime(2026, 11, 14, 18, 0, tzinfo=UTC),
            "kind": "recipe_rating",
            "recipe_id": r1,
            "structured": {"rating": 4},
        },
        # Boolean rating must not be treated as int rating 1
        {
            "id": uuid4(),
            "occurred_at": datetime(2026, 11, 13, 18, 0, tzinfo=UTC),
            "kind": "recipe_rating",
            "recipe_id": r1,
            "structured": {"rating": True},
        },
        # Confirmed cooking via kind='cooked'
        {
            "id": uuid4(),
            "occurred_at": datetime(2026, 11, 12, 18, 0, tzinfo=UTC),
            "kind": "cooked",
            "recipe_id": r1,
            "structured": {},
        },
        # Confirmed cooking via domain_event + JSON string structured with recipe_id
        {
            "id": uuid4(),
            "occurred_at": datetime(2026, 11, 11, 18, 0, tzinfo=UTC),
            "kind": "domain_event",
            "recipe_id": None,
            "structured": json.dumps({"recipe_id": str(r1), "action": "cooking_completed"}),
        },
        # Planning event must NEVER count as cooked even if action mentions cook
        {
            "id": uuid4(),
            "occurred_at": datetime(2026, 11, 10, 18, 0, tzinfo=UTC),
            "kind": "planning",
            "recipe_id": r1,
            "structured": {"action": "recipe_cooked"},
        },
        # Purchase traced to r1 + domain_event order traced to r2
        {
            "id": uuid4(),
            "occurred_at": datetime(2026, 11, 9, 18, 0, tzinfo=UTC),
            "kind": "purchase",
            "recipe_id": r1,
            "structured": {},
        },
        {
            "id": uuid4(),
            "occurred_at": datetime(2026, 11, 8, 18, 0, tzinfo=UTC),
            "kind": "domain_event",
            "recipe_id": r2,
            "structured": {"event": "oda_order"},
        },
        # Unrelated recipe filtered out when recipe_ids is passed
        {
            "id": uuid4(),
            "occurred_at": datetime(2026, 11, 7, 18, 0, tzinfo=UTC),
            "kind": "cooked",
            "recipe_id": other,
            "structured": {},
        },
    ]

    store = _FakeStatsStore(
        meal_rows=meal_rows,
        history_rows=history_rows,
        recipe_rows=[{"id": r1}, {"id": r2}, {"id": r_unused}],
    )

    stats = store.get_recipes_activity_stats([r1, r1, r2, r_unused])
    assert set(stats) == {r1, r2, r_unused}
    assert stats[r1] == RecipeActivityStats(
        times_planned=4,
        times_cooked=2,
        times_ordered=1,
        latest_rating=5,
        average_rating=4.5,
        would_make_again=True,
        feedback_count=4,
    )
    assert stats[r2] == RecipeActivityStats(times_planned=1, times_ordered=1)
    assert stats[r_unused] == RecipeActivityStats()
    assert store.last_connection is not None
    assert store.last_connection.queries[0][1] == {"ids": [r1, r2, r_unused]}

    assert store.get_recipes_activity_stats([]) == {}
    assert store.last_connection.queries == []

    all_stats = store.get_recipes_activity_stats()
    assert other in all_stats
    assert all_stats[other].times_cooked == 1


def test_rest_stats_endpoints_and_quick_feedback_loop():
    service, store = _make_memory_service()
    recipe = _sample_recipe()
    other_recipe = _sample_recipe("Tomatsuppe")
    store.save("recipe", recipe)
    store.save("recipe", other_recipe)
    store.planned_counts[recipe.id] = 3
    store.cooked_counts[recipe.id] = 2
    store.ordered_counts[recipe.id] = 1

    app = create_app(
        Settings(local_dev=True, database_url="postgresql+psycopg://localhost/kitchen_test"),
        service=service,
    )
    history = _InMemoryHistory(store)
    app.state.features.history = history  # type: ignore[assignment]
    app.state.features.recipe_feedback.history = history  # type: ignore[assignment]

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        # Default list/get omit stats
        listed_default = client.get("/api/recipes").json()
        assert listed_default[0]["activity_stats"] is None
        assert listed_default[0]["stats"] is None

        # 1-click rating + 1-click thumbs down
        r_star = client.post(f"/api/recipes/{recipe.id}/feedback", json={"rating": 4})
        assert r_star.status_code == 201
        r_thumb = client.post(f"/api/recipes/{recipe.id}/feedback", json={"would_make_again": False})
        assert r_thumb.status_code == 201

        single = client.get(f"/api/recipes/{recipe.id}/stats")
        assert single.status_code == 200
        assert single.json() == {
            "times_planned": 3,
            "times_cooked": 2,
            "times_ordered": 1,
            "latest_rating": 4,
            "average_rating": 4.0,
            "would_make_again": False,
            "feedback_count": 2,
        }

        batch = client.get(
            "/api/recipes/stats",
            params=[("recipe_id", str(recipe.id)), ("recipe_id", str(other_recipe.id))],
        )
        assert batch.status_code == 200
        assert batch.json()[str(recipe.id)]["latest_rating"] == 4
        assert batch.json()[str(other_recipe.id)]["feedback_count"] == 0

        with_include_stats = client.get("/api/recipes", params={"include_stats": "true"}).json()
        assert with_include_stats[0]["activity_stats"]["times_planned"] == 3
        assert with_include_stats[0]["stats"]["would_make_again"] is False

        single_with_stats = client.get(f"/api/recipes/{recipe.id}", params={"stats": "true"}).json()
        assert single_with_stats["activity_stats"]["average_rating"] == 4.0

        missing_id = uuid4()
        assert client.get(f"/api/recipes/{missing_id}/stats").status_code == 404
        assert client.get(f"/api/recipes/{missing_id}", params={"stats": "true"}).status_code == 404


@pytest.mark.asyncio
async def test_mcp_get_recipe_stats_and_quick_feedback():
    service, store = _make_memory_service()
    recipe = _sample_recipe()
    store.save("recipe", recipe)
    store.planned_counts[recipe.id] = 2
    store.cooked_counts[recipe.id] = 1

    features = KitchenFeatures(
        service,
        Settings(local_dev=True, database_url="postgresql+psycopg://localhost/kitchen_test"),
        Household,
    )
    history = _InMemoryHistory(store)
    features.history = history  # type: ignore[assignment]
    features.recipe_feedback.history = history  # type: ignore[assignment]

    mcp = build_mcp(service, Household)
    install_feature_tools(mcp, features, BasketService(service, features.history))

    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert "get_recipe_stats" in tools
        assert tools["get_recipe_stats"].annotations.readOnlyHint is True
        assert tools["get_recipe_stats"].annotations.idempotentHint is True
        assert tools["get_recipe_stats"].annotations.destructiveHint is False

        await client.call_tool(
            "record_recipe_feedback",
            {"recipe_id": str(recipe.id), "data": {"rating": 5}},
        )
        await client.call_tool(
            "record_recipe_feedback",
            {"recipe_id": str(recipe.id), "data": {"would_make_again": True}},
        )

        stats_result = await client.call_tool("get_recipe_stats", {"recipe_id": str(recipe.id)})
        stats = stats_result.structured_content
        stats = stats.get("result", stats)
        assert stats == {
            "times_planned": 2,
            "times_cooked": 1,
            "times_ordered": 0,
            "latest_rating": 5,
            "average_rating": 5.0,
            "would_make_again": True,
            "feedback_count": 2,
        }


async def test_postgres_recipe_stats_and_quick_feedback_end_to_end(test_database_url):
    from datetime import date

    from sqlalchemy import text

    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        recipe = client.post(
            "/api/recipes",
            json={
                "title": "Laks med grønnsaker",
                "servings": 2,
                "ingredients": [{"name": "Laks", "quantity": 300, "unit": "g"}],
                "instructions": ["Stek laks"],
            },
        ).json()
        recipe_id = UUID(recipe["id"])

        # Planning a meal increments times_planned only, never times_cooked
        meal_resp = client.post(
            "/api/meals",
            json={
                "date": date(2026, 11, 20).isoformat(),
                "recipe_id": str(recipe_id),
                "servings": 2,
            },
        )
        assert meal_resp.status_code == 201
        assert MealCreate  # keep import used when integration runs

        initial_stats = client.get(f"/api/recipes/{recipe_id}/stats").json()
        assert initial_stats["times_planned"] == 1
        assert initial_stats["times_cooked"] == 0
        assert initial_stats["times_ordered"] == 0

        # Record confirmed cooking and purchase history rows
        with app.state.service.store.connection() as conn:
            conn.execute(
                text(
                    "INSERT INTO kitchen_history "
                    "(id, occurred_at, channel, kind, message, structured, recipe_id) "
                    "VALUES (:id, now(), 'app', 'cooked', 'Cooked meal', '{}'::jsonb, :recipe_id)"
                ),
                {"id": uuid4(), "recipe_id": recipe_id},
            )
            conn.execute(
                text(
                    "INSERT INTO kitchen_history "
                    "(id, occurred_at, channel, kind, message, structured, recipe_id) "
                    "VALUES (:id, now(), 'api', 'purchase', 'Ordered', '{}'::jsonb, :recipe_id)"
                ),
                {"id": uuid4(), "recipe_id": recipe_id},
            )

        # 1-click feedback sequence
        assert (
            client.post(f"/api/recipes/{recipe_id}/feedback", json={"rating": 3}).status_code == 201
        )
        assert (
            client.post(f"/api/recipes/{recipe_id}/feedback", json={"rating": 5}).status_code == 201
        )
        assert (
            client.post(
                f"/api/recipes/{recipe_id}/feedback", json={"would_make_again": True}
            ).status_code
            == 201
        )

        final_stats = client.get(f"/api/recipes/{recipe_id}/stats").json()
        assert final_stats == {
            "times_planned": 1,
            "times_cooked": 1,
            "times_ordered": 1,
            "latest_rating": 5,
            "average_rating": 4.0,
            "would_make_again": True,
            "feedback_count": 3,
        }

        detailed = client.get(f"/api/recipes/{recipe_id}", params={"include_stats": "true"}).json()
        assert detailed["activity_stats"] == final_stats
        assert detailed["stats"] == final_stats
