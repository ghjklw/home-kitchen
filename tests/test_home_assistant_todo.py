"""Offline tests for the optional one-way Home Assistant To-Do adapter."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import SecretStr, ValidationError

from home_kitchen.app import create_app
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.home_assistant import (
    HomeAssistantAuthError,
    HomeAssistantError,
    HomeAssistantTodoClient,
)
from home_kitchen.settings import Settings


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
