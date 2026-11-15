from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from home_kitchen.app import create_app
from home_kitchen.basket.matching import BasketMatchResult
from home_kitchen.basket.repository import BasketService
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.models import ShoppingItemCreate
from home_kitchen.recipes.discovery import RecipeDiscoveryResponse
from home_kitchen.settings import Settings

START = "2026-11-16"
END = "2026-11-22"


def app_for(database_url, *, local_dev=True):
    return create_app(Settings(local_dev=local_dev, database_url=database_url))


def read_only_state(client):
    return {
        "items": client.get("/api/items").json(),
        "recipes": client.get("/api/recipes").json(),
        "basket": client.get("/api/basket", params={"start": START, "end": END}).json(),
    }


def mcp_read_only_state(app):
    basket = BasketService(app.state.service, app.state.features.history)
    return (
        app.state.features.kitchen.list_items(),
        app.state.features.kitchen.list_recipes(),
        basket.review(date.fromisoformat(START), date.fromisoformat(END)),
    )


def test_recipe_discovery_rest_contract_and_read_only(test_database_url):
    app = app_for(test_database_url)
    search = AsyncMock(
        return_value=RecipeDiscoveryResponse(query="lentil soup", results=[], sources=[])
    )
    app.state.features.recipe_discovery.search = search

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        before = read_only_state(client)
        response = client.post(
            "/api/recipe-discovery",
            json={"query": "lentil soup", "sources": ["marmiton"], "limit": 3},
        )

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert search.await_args.args[0].query == "lentil soup"
        assert search.await_args.kwargs == {"actor_id": "local-development"}
        assert read_only_state(client) == before

        assert (
            client.post("/api/recipe-discovery", json={"query": "soup", "limit": 0}).status_code
            == 422
        )
        assert (
            client.post(
                "/api/recipe-discovery", json={"query": "soup", "sources": ["unknown"]}
            ).status_code
            == 422
        )


@pytest.mark.asyncio
async def test_recipe_discovery_mcp_tool_contract_and_read_only(test_database_url):
    app = app_for(test_database_url)
    search = AsyncMock(return_value=RecipeDiscoveryResponse(query="pasta", results=[], sources=[]))
    app.state.features.recipe_discovery.search = search
    before = mcp_read_only_state(app)
    token = history_context.set(HistoryContext(actor_id="mcp-household", channel="mcp"))
    try:
        async with Client(app.state.mcp) as client:
            result = await client.call_tool(
                "find_recipes", {"request": {"query": "pasta", "sources": ["matprat"]}}
            )
        assert not result.is_error
        assert search.await_args.args[0].query == "pasta"
        assert search.await_args.kwargs == {"actor_id": "mcp-household"}
        assert mcp_read_only_state(app) == before
    finally:
        history_context.reset(token)


def test_basket_matching_rest_contract_validation_auth_and_read_only(test_database_url):
    app = app_for(test_database_url)
    matching = AsyncMock(
        return_value=BasketMatchResult(
            start=date.fromisoformat(START), end=date.fromisoformat(END), needs=[]
        )
    )
    app.state.features.basket_matching.find_products = matching

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        client.post(
            "/api/items", json={"name": "Tomatoes", "quantity": 4, "unit": "item"}
        ).raise_for_status()
        before = read_only_state(client)
        response = client.get(
            "/api/basket/matches", params={"start": START, "end": END, "page": 2, "size": 3}
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert matching.await_args.args == (
            "local-development",
            date.fromisoformat(START),
            date.fromisoformat(END),
        )
        assert matching.await_args.kwargs == {"page": 2, "size": 3}
        assert read_only_state(client) == before

        for params in (
            {"start": START, "end": END, "page": 0},
            {"start": START, "end": END, "size": 6},
            {"start": "not-a-date", "end": END},
        ):
            assert client.get("/api/basket/matches", params=params).status_code == 422

    unauthenticated = create_app(
        Settings(
            local_dev=False,
            database_url=test_database_url,
            entra_tenant_id="00000000-0000-0000-0000-000000000001",
            entra_audience="00000000-0000-0000-0000-000000000002",
            entra_allowed_group="00000000-0000-0000-0000-000000000003",
        )
    )
    with TestClient(unauthenticated, client=("127.0.0.1", 5000)) as client:
        assert (
            client.get("/api/basket/matches", params={"start": START, "end": END}).status_code
            == 401
        )


@pytest.mark.asyncio
async def test_basket_matching_mcp_tool_contract_and_read_only(test_database_url):
    app = app_for(test_database_url)
    matching = AsyncMock(
        return_value=BasketMatchResult(
            start=date.fromisoformat(START), end=date.fromisoformat(END), needs=[]
        )
    )
    app.state.features.basket_matching.find_products = matching
    app.state.features.kitchen.add_item(
        ShoppingItemCreate(name="Tomatoes", quantity=4, unit="item")
    )
    before = mcp_read_only_state(app)
    token = history_context.set(HistoryContext(actor_id="mcp-household", channel="mcp"))
    try:
        async with Client(app.state.mcp) as client:
            result = await client.call_tool(
                "find_basket_products", {"start": START, "end": END, "page": 2, "size": 3}
            )
        assert not result.is_error
        assert matching.await_args.args == (
            "mcp-household",
            date.fromisoformat(START),
            date.fromisoformat(END),
        )
        assert matching.await_args.kwargs == {"page": 2, "size": 3}
        assert mcp_read_only_state(app) == before
    finally:
        history_context.reset(token)
