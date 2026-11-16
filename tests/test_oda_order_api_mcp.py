"""Actor-scoped order reads do not invoke cart writes or purchase ingestion."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from home_kitchen.context import HistoryContext, history_context
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.oda import OdaError
from home_kitchen.oda_api import oda_router


@pytest.fixture
def oda():
    return SimpleNamespace(
        list_orders=AsyncMock(return_value={"hasMore": True, "orders": []}),
        track_order=AsyncMock(return_value={"orderNumber": "000123", "status": "unpaid_order"}),
    )


def api(oda, actor_id="alice"):
    app = FastAPI()
    app.include_router(oda_router(oda, cart_service=Mock()))

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.actor_id = actor_id
        return await call_next(request)

    return TestClient(app)


def test_rest_reads_preserve_provider_values_and_actor(oda):
    with api(oda, "bob") as client:
        orders = client.get("/api/oda/orders", params={"page": 50, "size": 1})
        tracking = client.get("/api/oda/orders/000123/tracking")
    assert orders.status_code == tracking.status_code == 200
    assert orders.json() == {"hasMore": True, "orders": []}
    assert tracking.json() == {"orderNumber": "000123", "status": "unpaid_order"}
    for response in (orders, tracking):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
    oda.list_orders.assert_awaited_once_with("bob", 50, 1)
    oda.track_order.assert_awaited_once_with("bob", "000123")


def test_rest_defaults_and_bounds(oda):
    with api(oda) as client:
        assert client.get("/api/oda/orders").status_code == 200
        oda.list_orders.assert_awaited_once_with("alice", 1, 20)
        oda.list_orders.reset_mock()
        for params in ({"page": 0}, {"page": 51}, {"size": 0}, {"size": 21}):
            assert client.get("/api/oda/orders", params=params).status_code == 422
        assert client.get(f"/api/oda/orders/{'x' * 513}/tracking").status_code == 422
    oda.list_orders.assert_not_awaited()
    oda.track_order.assert_not_awaited()


@pytest.mark.parametrize("path", ["/api/oda/orders", "/api/oda/orders/000123/tracking"])
def test_rest_requires_actor_and_maps_service_failure(oda, path):
    with api(oda, None) as client:
        assert client.get(path).status_code == 401
    oda.list_orders.assert_not_awaited()
    oda.track_order.assert_not_awaited()
    oda.list_orders.side_effect = OdaError("Reconnect Oda", 409)
    oda.track_order.side_effect = OdaError("Reconnect Oda", 409)
    with api(oda) as client:
        response = client.get(path)
    assert response.status_code == 409
    assert response.json() == {"detail": "Reconnect Oda"}
    assert response.headers["cache-control"] == "no-store"


def mcp_server(oda):
    mcp = FastMCP("Order reads")
    install_feature_tools(mcp, SimpleNamespace(oda=oda), Mock())
    return mcp


@pytest.mark.asyncio
async def test_mcp_read_contracts_actor_and_exact_values(oda):
    token = history_context.set(HistoryContext(actor_id="bob", channel="mcp"))
    try:
        async with Client(mcp_server(oda)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            for name in ("list_oda_orders", "track_oda_order"):
                assert tools[name].annotations.readOnlyHint is True
                assert tools[name].annotations.openWorldHint is True
                assert "purchase evidence" in tools[name].description
            properties = tools["list_oda_orders"].inputSchema["properties"]
            assert properties["page"]["maximum"] == 50
            assert properties["size"]["maximum"] == 20
            orders = await client.call_tool("list_oda_orders", {"page": 2, "size": 3})
            tracking = await client.call_tool("track_oda_order", {"order_number": "000123"})
            assert orders.structured_content == {"hasMore": True, "orders": []}
            assert tracking.structured_content == {
                "orderNumber": "000123",
                "status": "unpaid_order",
            }
        oda.list_orders.assert_awaited_once_with("bob", 2, 3)
        oda.track_order.assert_awaited_once_with("bob", "000123")
    finally:
        history_context.reset(token)


@pytest.mark.asyncio
async def test_mcp_requires_actor_and_rejects_out_of_bounds_inputs(oda):
    token = history_context.set(HistoryContext())
    try:
        async with Client(mcp_server(oda)) as client:
            for name, arguments in (
                ("list_oda_orders", {}),
                ("track_oda_order", {"order_number": "000123"}),
            ):
                with pytest.raises(ToolError, match="Household authentication required"):
                    await client.call_tool(name, arguments)
            for arguments in ({"page": 0}, {"page": 51}, {"size": 0}, {"size": 21}):
                with pytest.raises(ToolError):
                    await client.call_tool("list_oda_orders", arguments)
            for order_number in ("", "x" * 513):
                with pytest.raises(ToolError):
                    await client.call_tool("track_oda_order", {"order_number": order_number})
        oda.list_orders.assert_not_awaited()
        oda.track_order.assert_not_awaited()
    finally:
        history_context.reset(token)
