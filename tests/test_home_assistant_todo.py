"""Offline tests for the optional Home Assistant To-Do adapter."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import SecretStr, ValidationError

from home_kitchen.app import create_app
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.features import KitchenFeatures
from home_kitchen.home_assistant import (
    HomeAssistantAuthError,
    HomeAssistantError,
    HomeAssistantNotConfiguredError,
    HomeAssistantTodoClient,
)
from home_kitchen.models import ShoppingItem, ShoppingItemCreate, ShoppingLine, WeekShopping
from home_kitchen.purchasing import StoreTrip, StoreTripLine
from home_kitchen.settings import Settings

pytestmark = pytest.mark.mocked_provider


def _ha_mock_transport(
    state_items: list[dict],
    calls: list[tuple[str, str, dict | None]],
    *,
    expected_token: str = "ha-secret-token",
    use_state_fallback: bool = False,
    force_status: int | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        calls.append((request.method, str(request.url), body))

        if request.headers.get("authorization") != f"Bearer {expected_token}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        if force_status is not None:
            return httpx.Response(force_status, json={"message": "Forced error"})

        path = request.url.path
        if path == "/api/services/todo/get_items":
            if use_state_fallback:
                return httpx.Response(404, json={"message": "Not Found"})
            entity_id = (body or {}).get("entity_id", "todo.shopping_list")
            return httpx.Response(
                200,
                json={
                    "changed_states": [],
                    "service_response": {entity_id: {"items": list(state_items)}},
                },
            )
        if path.startswith("/api/states/"):
            return httpx.Response(
                200,
                json={
                    "entity_id": path.removeprefix("/api/states/"),
                    "state": str(len(state_items)),
                    "attributes": {"items": list(state_items)},
                },
            )
        if path == "/api/services/todo/add_item":
            item_summary = (body or {}).get("item")
            state_items.append({"summary": item_summary, "status": "needs_action"})
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "Unknown endpoint"})

    return httpx.MockTransport(handler)


def test_home_assistant_client_deduplicates_existing_active_items():
    ha_items = [
        {"summary": "  Milk (2 l)  ", "status": "needs_action"},
        {"summary": "Coffee", "status": "completed"},
    ]
    calls: list[tuple[str, str, dict | None]] = []
    client = HomeAssistantTodoClient(
        "https://ha.local:8123/",
        "ha-secret-token",
        entity_id="todo.groceries",
        transport=_ha_mock_transport(ha_items, calls),
    )

    result = client.export_remaining_items(
        ["milk (2 l)", "Bread", "bread", "Coffee", "  "],
    )

    assert result == {
        "added": ["Bread", "Coffee"],
        "skipped_existing": ["milk (2 l)"],
        "total_requested": 3,
        "entity_id": "todo.groceries",
    }
    add_calls = [c for c in calls if c[1].endswith("/api/services/todo/add_item")]
    assert [c[2] for c in add_calls] == [
        {"entity_id": "todo.groceries", "item": "Bread"},
        {"entity_id": "todo.groceries", "item": "Coffee"},
    ]

    # Second call with same items produces zero duplicates.
    replay = client.export_remaining_items(["Milk (2 l)", "Bread", "Coffee"])
    assert replay == {
        "added": [],
        "skipped_existing": ["Milk (2 l)", "Bread", "Coffee"],
        "total_requested": 3,
        "entity_id": "todo.groceries",
    }


def test_home_assistant_client_dry_run_and_state_fallback():
    ha_items = [{"summary": "Eggs", "status": "needs_action"}]
    calls: list[tuple[str, str, dict | None]] = []
    client = HomeAssistantTodoClient(
        "http://homeassistant.local:8123",
        SecretStr("ha-secret-token"),
        transport=_ha_mock_transport(ha_items, calls, use_state_fallback=True),
    )

    listed = client.list_items()
    assert listed == [{"summary": "Eggs", "status": "needs_action"}]
    assert any("/api/states/todo.shopping_list" in c[1] for c in calls)

    dry = client.export_remaining_items(["eggs", "Butter"], dry_run=True)
    assert dry == {
        "added": ["Butter"],
        "skipped_existing": ["eggs"],
        "total_requested": 2,
        "entity_id": "todo.shopping_list",
    }
    assert not any(c[1].endswith("/api/services/todo/add_item") for c in calls)


@pytest.mark.parametrize("status_code", [401, 403])
def test_home_assistant_client_auth_failure_raises_auth_error(status_code):
    def auth_fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"message": "Forbidden"})

    client = HomeAssistantTodoClient(
        "https://ha.local:8123",
        "bad-token",
        transport=httpx.MockTransport(auth_fail_handler),
    )

    with pytest.raises(HomeAssistantAuthError):
        client.list_items()

    with pytest.raises(HomeAssistantAuthError):
        client.export_remaining_items(["Milk"])


def test_home_assistant_client_http_and_network_errors_raise_ha_error():
    ha_items: list[dict] = []
    calls: list[tuple[str, str, dict | None]] = []
    server_err_client = HomeAssistantTodoClient(
        "https://ha.local:8123",
        "ha-secret-token",
        transport=_ha_mock_transport(ha_items, calls, force_status=500),
    )
    with pytest.raises(HomeAssistantError):
        server_err_client.list_items()

    def network_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    net_err_client = HomeAssistantTodoClient(
        "https://ha.local:8123",
        "ha-secret-token",
        transport=httpx.MockTransport(network_fail),
    )
    with pytest.raises(HomeAssistantError):
        net_err_client.export_remaining_items(["Milk"])


def test_settings_home_assistant_fields_and_validation(monkeypatch):
    monkeypatch.setenv("KITCHEN_LOCAL_DEV", "true")
    monkeypatch.setenv("KITCHEN_DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("KITCHEN_HOME_ASSISTANT_URL", "http://homeassistant.local:8123")
    monkeypatch.setenv("KITCHEN_HOME_ASSISTANT_TOKEN", "ha-env-token")
    monkeypatch.setenv("KITCHEN_HOME_ASSISTANT_TODO_ENTITY", " todo.kitchen_list ")

    settings = Settings()
    assert str(settings.home_assistant_url).rstrip("/") == "http://homeassistant.local:8123"
    assert settings.home_assistant_token is not None
    assert settings.home_assistant_token.get_secret_value() == "ha-env-token"
    assert settings.home_assistant_todo_entity == "todo.kitchen_list"

    with pytest.raises(ValidationError):
        Settings(
            local_dev=True,
            database_url="postgresql+psycopg://u:p@localhost:5432/db",
            home_assistant_token=SecretStr("   "),
        )


def test_home_assistant_integration_connect_and_settings_fallback(test_database_url):
    app = create_app(
        Settings(
            local_dev=True,
            database_url=test_database_url,
            home_assistant_url="http://homeassistant.local:8123",
            home_assistant_token=SecretStr("env-ha-secret"),
        )
    )
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        integrations = {item["id"]: item for item in client.get("/api/integrations").json()}
        ha = integrations["home_assistant"]
        assert ha["name"] == "Home Assistant"
        assert ha["status"] == "configured"
        assert ha["supported_auth_modes"] == ["api_key"]
        assert ha["capabilities"] == ["todo_export"]
        assert "env-ha-secret" not in json.dumps(ha)

        connect_resp = client.post(
            "/api/integrations/home_assistant/connect",
            json={"api_key": "stored-ha-secret", "entity_id": "todo.family"},
        )
        assert connect_resp.status_code == 200, connect_resp.text
        connected = connect_resp.json()
        assert connected["status"] == "connected"
        assert connected["auth_mode"] == "api_key"
        assert "stored-ha-secret" not in connect_resp.text

        disconnect_resp = client.delete("/api/integrations/home_assistant/connection")
        assert disconnect_resp.status_code == 204
        after = {item["id"]: item for item in client.get("/api/integrations").json()}
        assert after["home_assistant"]["status"] == "configured"


@pytest.mark.asyncio
async def test_rest_and_mcp_export_require_review_and_handle_unconfigured_ha(test_database_url):
    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    period = {"start": "2026-12-01", "end": "2026-12-07"}

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        unreviewed = client.post(
            "/api/store-trip/export/home-assistant",
            json={**period, "reviewed": False},
        )
        assert unreviewed.status_code == 422

        missing_review = client.post("/api/store-trip/export/home-assistant", json=period)
        assert missing_review.status_code == 422

        unconfigured = client.post(
            "/api/store-trip/export/home-assistant",
            json={**period, "reviewed": True},
        )
        assert unconfigured.status_code == 503

        async with Client(app.state.mcp) as mcp:
            tools = {tool.name for tool in await mcp.list_tools()}
            assert "export_store_trip_to_home_assistant" in tools

        token = history_context.set(HistoryContext(actor_id="alice", channel="mcp"))
        try:
            with pytest.raises(ToolError, match="reviewed=true"):
                await app.state.mcp.call_tool(
                    "export_store_trip_to_home_assistant",
                    {**period, "reviewed": False},
                )
            with pytest.raises(ToolError, match="ikke konfigurert"):
                await app.state.mcp.call_tool(
                    "export_store_trip_to_home_assistant",
                    {**period, "reviewed": True},
                )
        finally:
            history_context.reset(token)


@pytest.mark.asyncio
async def test_rest_and_mcp_export_remaining_store_trip_items_idempotently(test_database_url):
    ha_items = [{"summary": "Coffee", "status": "needs_action"}]
    calls: list[tuple[str, str, dict | None]] = []

    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    app.state.features.home_assistant_transport = _ha_mock_transport(
        ha_items, calls, expected_token="stored-ha-token"
    )
    today = date.today().isoformat()
    period = {"start": today, "end": today}

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        connect_resp = client.post(
            "/api/integrations/home_assistant/connect",
            json={
                "api_key": "stored-ha-token",
                "base_url": "http://ha.local:8123",
                "entity_id": "todo.weekly",
            },
        )
        assert connect_resp.status_code == 200

        client.post(
            "/api/items", json={"name": "Milk", "quantity": "2", "unit": "l"}
        ).raise_for_status()
        client.post("/api/items", json={"name": "Coffee"}).raise_for_status()
        client.post("/api/items", json={"name": "Bread"}).raise_for_status()

        trip = client.get("/api/store-trip", params=period).json()
        bread = next(line for line in trip["lines"] if line["name"] == "Bread")
        # Picked items must not be exported.
        client.put(
            f"/api/store-trip/items/{bread['id']}",
            params=period,
            json={"checked": True},
        ).raise_for_status()

        exported = client.post(
            "/api/store-trip/export/home-assistant",
            json={**period, "reviewed": True},
        )
        assert exported.status_code == 200, exported.text
        assert exported.json() == {
            "added": ["Milk (2000 ml)"],
            "skipped_existing": ["Coffee"],
            "total_requested": 2,
            "entity_id": "todo.weekly",
        }

        # Re-exporting via MCP is idempotent and adds nothing new.
        token = history_context.set(HistoryContext(actor_id="alice", channel="mcp"))
        try:
            mcp_res = await app.state.mcp.call_tool(
                "export_store_trip_to_home_assistant",
                {**period, "reviewed": True},
            )
            assert mcp_res.structured_content == {
                "added": [],
                "skipped_existing": ["Coffee", "Milk (2000 ml)"],
                "total_requested": 2,
                "entity_id": "todo.weekly",
            }
        finally:
            history_context.reset(token)

        # Kitchen StoreTrip remains canonical and unchanged by the export.
        trip_after = client.get("/api/store-trip", params=period).json()
        assert trip_after["checked_count"] == 1
        assert trip_after["total_count"] == 3


@pytest.mark.asyncio
async def test_rest_and_mcp_export_surface_auth_failures_cleanly(test_database_url):
    app = create_app(
        Settings(
            local_dev=True,
            database_url=test_database_url,
            home_assistant_url="http://ha.local:8123",
            home_assistant_token=SecretStr("bad-token"),
        )
    )
    app.state.features.home_assistant_transport = _ha_mock_transport(
        [], [], expected_token="expected-token"
    )
    today = date.today().isoformat()
    period = {"start": today, "end": today}

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        client.post("/api/items", json={"name": "Milk"}).raise_for_status()
        resp = client.post(
            "/api/store-trip/export/home-assistant",
            json={**period, "reviewed": True},
        )
        assert resp.status_code == 502

        token = history_context.set(HistoryContext(actor_id="alice", channel="mcp"))
        try:
            with pytest.raises(ToolError, match="Ugyldig eller utløpt Home Assistant-token"):
                await app.state.mcp.call_tool(
                    "export_store_trip_to_home_assistant",
                    {**period, "reviewed": True},
                )
        finally:
            history_context.reset(token)


def test_home_assistant_client_preview_and_import_quick_captures_dedup_and_active_only():
    ha_items = [
        {"summary": "  Avocados  ", "status": "needs_action"},
        {"summary": "avocados", "status": "needs_action"},
        {"summary": "Milk", "status": "needs_action"},
        {"summary": "Old Coffee", "status": "completed"},
        {"summary": "Done Tea", "complete": True},
        {"summary": "Oat milk", "status": "needs_action"},
    ]
    calls: list[tuple[str, str, dict | None]] = []
    client = HomeAssistantTodoClient(
        "https://ha.local:8123/",
        "ha-secret-token",
        entity_id="todo.groceries",
        transport=_ha_mock_transport(ha_items, calls),
    )

    preview = client.preview_quick_captures(["  milk  "])
    assert preview == {
        "reviewed": False,
        "candidates": ["Avocados", "Oat milk"],
        "added": [],
        "created_items": [],
        "skipped_existing": ["avocados", "Milk"],
        "total_active_in_ha": 4,
        "entity_id": "todo.groceries",
    }

    unreviewed = client.import_quick_captures(["milk"], reviewed=False)
    assert unreviewed == preview

    confirmed = client.import_quick_captures(["milk"], reviewed=True)
    assert confirmed == {
        "reviewed": True,
        "candidates": ["Avocados", "Oat milk"],
        "added": ["Avocados", "Oat milk"],
        "created_items": [],
        "skipped_existing": ["avocados", "Milk"],
        "total_active_in_ha": 4,
        "entity_id": "todo.groceries",
    }
    assert not any(c[1].endswith("/api/services/todo/add_item") for c in calls)


def test_home_assistant_client_quick_captures_state_fallback_and_errors():
    ha_items = [
        {"summary": "Eggs", "status": "needs_action"},
        {"summary": "Done Butter", "status": "completed"},
    ]
    calls: list[tuple[str, str, dict | None]] = []
    fallback_client = HomeAssistantTodoClient(
        "http://homeassistant.local:8123",
        SecretStr("ha-secret-token"),
        transport=_ha_mock_transport(ha_items, calls, use_state_fallback=True),
    )

    preview = fallback_client.preview_quick_captures([])
    assert preview["candidates"] == ["Eggs"]
    assert preview["total_active_in_ha"] == 1
    assert any("/api/states/todo.shopping_list" in c[1] for c in calls)

    auth_err_client = HomeAssistantTodoClient(
        "https://ha.local:8123",
        "bad-token",
        transport=_ha_mock_transport(ha_items, [], expected_token="expected"),
    )
    with pytest.raises(HomeAssistantAuthError):
        auth_err_client.preview_quick_captures([])

    def network_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    net_err_client = HomeAssistantTodoClient(
        "https://ha.local:8123",
        "ha-secret-token",
        transport=httpx.MockTransport(network_fail),
    )
    with pytest.raises(HomeAssistantError):
        net_err_client.import_quick_captures([], reviewed=True)


@pytest.mark.asyncio
async def test_features_rest_and_mcp_quick_capture_preview_and_import_offline():
    milk_id = uuid4()
    apples_id = uuid4()
    pasta_need_id = uuid4()
    milk_need_id = uuid4()
    today = date.today()

    captures: list[ShoppingItem] = [
        ShoppingItem(id=milk_id, name="Milk", quantity=Decimal("2"), unit="l", completed=False),
        ShoppingItem(
            id=apples_id, name="Apples", quantity=Decimal("1"), unit="item", completed=True
        ),
    ]

    class FakeKitchen:
        def list_items(self) -> list[ShoppingItem]:
            return list(captures)

        def add_item(self, data: ShoppingItemCreate) -> ShoppingItem:
            item = ShoppingItem(id=uuid4(), **data.model_dump())
            captures.append(item)
            return item

        def shopping_list(self, start: date, end: date, today: date) -> WeekShopping:
            return WeekShopping(
                lines=[
                    ShoppingLine(
                        name="Pasta",
                        quantity=Decimal("400"),
                        unit="g",
                        origins=["meal:1"],
                    ),
                    *[
                        ShoppingLine(
                            name=item.name,
                            quantity=item.quantity,
                            unit=item.unit,
                            origins=[f"item:{item.id}"],
                        )
                        for item in captures
                        if not item.completed
                    ],
                ],
                suggestions=[],
                unquantified=[],
            )

    store_trip_lines = [
        StoreTripLine(
            id=milk_need_id,
            name="Milk",
            unit="ml",
            required_quantity=Decimal("2000"),
            available_quantity=Decimal("0"),
            covered_quantity=Decimal("0"),
            to_buy_quantity=Decimal("2000"),
            origins=[f"item:{milk_id}"],
            checked=True,
            checked_quantity=Decimal("2000"),
        ),
        StoreTripLine(
            id=pasta_need_id,
            name="Pasta",
            unit="g",
            required_quantity=Decimal("400"),
            available_quantity=Decimal("0"),
            covered_quantity=Decimal("0"),
            to_buy_quantity=Decimal("400"),
            origins=["meal:1"],
            checked=False,
            checked_quantity=Decimal("0"),
        ),
    ]

    class FakeStoreTrip:
        engine = object()

        def review(self, start: date, end: date) -> StoreTrip:
            return StoreTrip(
                start=start,
                end=end,
                lines=list(store_trip_lines),
                checked_count=1,
                total_count=2,
                unquantified=[],
            )

    ha_items = [
        {"summary": "milk (2000 ml)", "status": "needs_action"},
        {"summary": "PASTA (400 g)", "status": "needs_action"},
        {"summary": "Apples", "status": "needs_action"},
        {"summary": "Bananas", "status": "needs_action"},
        {"summary": "Done Item", "status": "completed"},
    ]
    calls: list[tuple[str, str, dict | None]] = []

    features = object.__new__(KitchenFeatures)
    features.kitchen = FakeKitchen()  # type: ignore[assignment]
    features.store_trip = FakeStoreTrip()  # type: ignore[assignment]
    features.history = None  # type: ignore[assignment]
    features.settings = Settings(
        local_dev=True,
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
    )
    features.home_assistant_transport = _ha_mock_transport(ha_items, calls)
    features.home_assistant_client = HomeAssistantTodoClient(
        "http://ha.local:8123",
        "ha-secret-token",
        entity_id="todo.kitchen",
        transport=features.home_assistant_transport,
    )

    # Direct KitchenFeatures preview & reviewed=False gate do not mutate Kitchen.
    direct_preview = features.preview_home_assistant_quick_captures(start=today, end=today)
    assert direct_preview.reviewed is False
    assert direct_preview.candidates == ["Apples", "Bananas"]
    assert direct_preview.added == []
    assert direct_preview.created_items == []
    assert direct_preview.skipped_existing == ["milk (2000 ml)", "PASTA (400 g)"]
    assert len(captures) == 2

    direct_unreviewed = features.import_home_assistant_quick_captures(
        reviewed=False, start=today, end=today
    )
    assert direct_unreviewed == direct_preview
    assert len(captures) == 2

    # Unauthenticated REST requests return 401.
    unauth_app = FastAPI()
    unauth_app.include_router(feature_router(features, None))  # type: ignore[arg-type]
    with TestClient(unauth_app) as unauth_client:
        assert unauth_client.post("/api/captures/import/home-assistant/preview").status_code == 401
        assert (
            unauth_client.post(
                "/api/captures/import/home-assistant", json={"reviewed": True}
            ).status_code
            == 401
        )

    # Authenticated REST app.
    app = FastAPI()

    @app.middleware("http")
    async def inject_actor(request: Request, call_next):
        request.state.actor_id = "alice"
        return await call_next(request)

    app.include_router(feature_router(features, None))  # type: ignore[arg-type]

    with TestClient(app) as client:
        preview_resp = client.post("/api/captures/import/home-assistant/preview")
        assert preview_resp.status_code == 200
        assert preview_resp.headers["cache-control"] == "no-store"
        assert preview_resp.json()["candidates"] == ["Apples", "Bananas"]
        assert preview_resp.json()["reviewed"] is False
        assert len(captures) == 2

        bad_dates = client.post(
            "/api/captures/import/home-assistant/preview",
            json={"start": "2026-12-10", "end": "2026-12-01"},
        )
        assert bad_dates.status_code == 422

        missing_review = client.post("/api/captures/import/home-assistant")
        assert missing_review.status_code == 422
        unreviewed_resp = client.post(
            "/api/captures/import/home-assistant", json={"reviewed": False}
        )
        assert unreviewed_resp.status_code == 422
        assert len(captures) == 2

        imported_resp = client.post(
            "/api/captures/import/home-assistant",
            json={"reviewed": True, "start": today.isoformat(), "end": today.isoformat()},
        )
        assert imported_resp.status_code == 200, imported_resp.text
        body = imported_resp.json()
        assert body["reviewed"] is True
        assert body["candidates"] == ["Apples", "Bananas"]
        assert body["added"] == ["Apples", "Bananas"]
        assert [item["name"] for item in body["created_items"]] == ["Apples", "Bananas"]
        assert all(item["completed"] is False for item in body["created_items"])
        assert body["skipped_existing"] == ["milk (2000 ml)", "PASTA (400 g)"]
        assert [c.name for c in captures] == ["Milk", "Apples", "Apples", "Bananas"]
        # Existing StoreTrip checked state remains canonical and untouched.
        assert store_trip_lines[0].checked is True
        assert store_trip_lines[0].checked_quantity == Decimal("2000")

        # Re-running is idempotent and creates no duplicates.
        replay_resp = client.post("/api/captures/import/home-assistant", json={"reviewed": True})
        assert replay_resp.status_code == 200
        assert replay_resp.json()["added"] == []
        assert replay_resp.json()["created_items"] == []
        assert len(captures) == 4

    # MCP preview & reviewed=True import.
    ha_items.append({"summary": "Oatmeal", "status": "needs_action"})
    mcp = FastMCP("test-ha")
    install_feature_tools(mcp, features, None)  # type: ignore[arg-type]

    token = history_context.set(HistoryContext(actor_id="alice", channel="mcp"))
    try:
        async with Client(mcp) as mcp_client:
            tools = {tool.name: tool for tool in await mcp_client.list_tools()}
            assert "preview_home_assistant_quick_captures" in tools
            assert "import_home_assistant_quick_captures" in tools
            assert tools["preview_home_assistant_quick_captures"].annotations.readOnlyHint is True
            assert tools["import_home_assistant_quick_captures"].annotations.readOnlyHint is False

            mcp_preview = await mcp_client.call_tool("preview_home_assistant_quick_captures", {})
            assert mcp_preview.structured_content["candidates"] == ["Oatmeal"]
            assert mcp_preview.structured_content["reviewed"] is False
            assert len(captures) == 4

            with pytest.raises(ToolError, match="reviewed=true"):
                await mcp_client.call_tool(
                    "import_home_assistant_quick_captures", {"reviewed": False}
                )
            assert len(captures) == 4

            mcp_import = await mcp_client.call_tool(
                "import_home_assistant_quick_captures", {"reviewed": True}
            )
            assert mcp_import.structured_content["added"] == ["Oatmeal"]
            assert [c.name for c in captures] == [
                "Milk",
                "Apples",
                "Apples",
                "Bananas",
                "Oatmeal",
            ]
    finally:
        history_context.reset(token)

    # Error mapping: unconfigured -> 503 REST / ToolError MCP; auth failure -> 502 REST / ToolError MCP.
    err_features = SimpleNamespace(
        preview_home_assistant_quick_captures=lambda **_: (_ for _ in ()).throw(
            HomeAssistantNotConfiguredError("HA ikke konfigurert")
        ),
        import_home_assistant_quick_captures=lambda **_: (_ for _ in ()).throw(
            HomeAssistantAuthError("Ugyldig token")
        ),
    )
    err_app = FastAPI()

    @err_app.middleware("http")
    async def inject_err_actor(request: Request, call_next):
        request.state.actor_id = "alice"
        return await call_next(request)

    err_app.include_router(feature_router(err_features, None))  # type: ignore[arg-type]
    with TestClient(err_app) as err_client:
        assert err_client.post("/api/captures/import/home-assistant/preview").status_code == 503
        assert (
            err_client.post(
                "/api/captures/import/home-assistant", json={"reviewed": True}
            ).status_code
            == 502
        )

    err_mcp = FastMCP("test-ha-err")
    install_feature_tools(err_mcp, err_features, None)  # type: ignore[arg-type]
    async with Client(err_mcp) as err_mcp_client:
        with pytest.raises(ToolError, match="HA ikke konfigurert"):
            await err_mcp_client.call_tool("preview_home_assistant_quick_captures", {})
        with pytest.raises(ToolError, match="Ugyldig token"):
            await err_mcp_client.call_tool(
                "import_home_assistant_quick_captures", {"reviewed": True}
            )


@pytest.mark.asyncio
async def test_rest_and_mcp_import_quick_captures_roundtrip_with_db(test_database_url):
    ha_items = [
        {"summary": "Coffee", "status": "needs_action"},
        {"summary": "Yogurt", "status": "needs_action"},
        {"summary": "Old Soap", "status": "completed"},
    ]
    calls: list[tuple[str, str, dict | None]] = []

    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    app.state.features.home_assistant_transport = _ha_mock_transport(
        ha_items, calls, expected_token="stored-ha-token"
    )
    today = date.today().isoformat()
    period = {"start": today, "end": today}

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        client.post(
            "/api/integrations/home_assistant/connect",
            json={
                "api_key": "stored-ha-token",
                "base_url": "http://ha.local:8123",
                "entity_id": "todo.weekly",
            },
        ).raise_for_status()

        client.post(
            "/api/items", json={"name": "Milk", "quantity": "2", "unit": "l"}
        ).raise_for_status()
        client.post("/api/items", json={"name": "Bread"}).raise_for_status()

        trip = client.get("/api/store-trip", params=period).json()
        bread = next(line for line in trip["lines"] if line["name"] == "Bread")
        client.put(
            f"/api/store-trip/items/{bread['id']}",
            params=period,
            json={"checked": True},
        ).raise_for_status()

        # Export Milk (2000 ml) to HA first.
        client.post(
            "/api/store-trip/export/home-assistant",
            json={**period, "reviewed": True},
        ).raise_for_status()

        # Preview inbound quick captures: Coffee & Yogurt are new; Milk (2000 ml) & Old Soap are excluded.
        preview = client.post("/api/captures/import/home-assistant/preview", json=period)
        assert preview.status_code == 200
        assert preview.json()["candidates"] == ["Coffee", "Yogurt"]
        assert preview.json()["skipped_existing"] == ["Milk (2000 ml)"]
        assert len(client.get("/api/items").json()) == 2

        # Confirm import with reviewed=True.
        imported = client.post(
            "/api/captures/import/home-assistant",
            json={**period, "reviewed": True},
        )
        assert imported.status_code == 200
        assert imported.json()["added"] == ["Coffee", "Yogurt"]

        items_after = client.get("/api/items").json()
        assert [item["name"] for item in items_after] == ["Milk", "Bread", "Coffee", "Yogurt"]

        # Bread remains checked in canonical StoreTrip; new captures join as unchecked lines.
        trip_after = client.get("/api/store-trip", params=period).json()
        bread_after = next(line for line in trip_after["lines"] if line["name"] == "Bread")
        assert bread_after["checked"] is True
        assert trip_after["checked_count"] == 1
        assert trip_after["total_count"] == 4

        # Re-importing via MCP is idempotent.
        token = history_context.set(HistoryContext(actor_id="alice", channel="mcp"))
        try:
            mcp_res = await app.state.mcp.call_tool(
                "import_home_assistant_quick_captures",
                {"reviewed": True, **period},
            )
            assert mcp_res.structured_content["added"] == []
            assert mcp_res.structured_content["candidates"] == []
        finally:
            history_context.reset(token)
