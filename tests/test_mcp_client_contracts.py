"""Wire-level MCP contracts for clients that consume only text content."""

import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from home_kitchen.basket.repository import BasketService
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.features import KitchenFeatures
from home_kitchen.mcp_server import build_mcp
from home_kitchen.models import Ingredient, MealCreate, RecipeCreate
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings


def _wire_call(client, request_id, name, arguments):
    response = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={
            "MCP-Protocol-Version": "2025-11-25",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 200, response.text
    payload = _payload(response)
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == request_id
    return payload["result"]


def _payload(response):
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = next(
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        )
        return json.loads(data)
    return response.json()


@pytest.mark.asyncio
async def test_streamable_http_tools_are_text_parseable_and_annotated(test_database_url):
    service = KitchenService(test_database_url)
    source = service.create_recipe(
        RecipeCreate(
            title="Wire contract soup",
            servings=2,
            ingredients=[Ingredient(name="Lentils", quantity=Decimal("100"), unit="g")],
            instructions=["Simmer for 20 minutes."],
        )
    )
    meal = service.plan_meal(MealCreate(date=date(2026, 11, 16), recipe_id=source.id, servings=4))
    features = KitchenFeatures(
        service,
        Settings(local_dev=True, database_url=test_database_url),
        Household,
    )
    mcp = build_mcp(service, Household)
    install_feature_tools(mcp, features, BasketService(service, features.history))
    asgi = mcp.http_app(path="/", stateless_http=True)

    with TestClient(asgi) as client:
        initialized = client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "compatibility-test", "version": "1"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert initialized.status_code == 200, initialized.text
        init_payload = _payload(initialized)
        assert init_payload["result"]["protocolVersion"] == "2025-11-25"

        listed = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={
                "MCP-Protocol-Version": "2025-11-25",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert listed.status_code == 200, listed.text
        tools = {tool["name"]: tool for tool in _payload(listed)["result"]["tools"]}
        for tool_name in ("get_planned_meal_recipe", "list_recipes", "find_foods"):
            assert tool_name in tools
            assert tools[tool_name].get("annotations")
        assert tools["list_recipes"]["annotations"]["readOnlyHint"] is True
        assert tools["save_recipe"]["annotations"]["readOnlyHint"] is False

        planned = _wire_call(
            client,
            3,
            "get_planned_meal_recipe",
            {"meal_id": str(meal.id)},
        )
        recipes = _wire_call(client, 4, "list_recipes", {})
        foods = _wire_call(client, 5, "find_foods", {"query": "laktosefri"})

        for result in (planned, recipes, foods):
            assert result["isError"] is False
            assert result["content"]
            assert all(item["type"] == "text" for item in result["content"])
            # Gemini's MCP consumers may ignore structuredContent; text must independently
            # contain valid JSON with the same result shape.
            decoded = json.loads(result["content"][0]["text"])
            assert decoded is not None
            assert "structuredContent" in result
            structured = result["structuredContent"]
            assert decoded == structured.get("result", structured)

        assert json.loads(planned["content"][0]["text"])["servings"] == 4
        assert len(json.loads(recipes["content"][0]["text"])) == 1
        assert json.loads(foods["content"][0]["text"])

        saved = _wire_call(
            client,
            6,
            "save_recipe",
            {
                "data": {
                    "title": "Controlled MCP wire fixture",
                    "servings": 1,
                    "ingredients": [{"name": "Water", "quantity": "1", "unit": "cup"}],
                    "instructions": ["Stir."],
                }
            },
        )
        assert saved["isError"] is False
        saved_text = json.loads(saved["content"][0]["text"])
        assert saved_text["title"] == "Controlled MCP wire fixture"
        assert service.get_recipe(saved_text["id"]).title == saved_text["title"]
