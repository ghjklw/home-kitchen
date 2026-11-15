import pytest
from fastmcp import Client

from home_kitchen.mcp_server import build_mcp
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household


def payload(result):
    content = result.structured_content
    return content.get("result", content)


@pytest.mark.asyncio
async def test_tools_share_service_and_persist_confirmed_suggestion(test_database_url):
    service = KitchenService(test_database_url)
    mcp = build_mcp(service, lambda: Household(name="Test home"))

    async with Client(mcp) as client:
        saved = await client.call_tool(
            "save_recipe",
            {
                "data": {
                    "title": "Oat porridge",
                    "servings": 2,
                    "ingredients": [{"name": "Oats", "quantity": 100, "unit": "g"}],
                    "instructions": ["Cook the oats"],
                }
            },
        )
        recipe_id = payload(saved)["id"]
        planned = await client.call_tool(
            "plan_meal",
            {
                "data": {
                    "date": "2026-01-05",
                    "slot": "breakfast",
                    "recipe_id": recipe_id,
                    "servings": 2,
                }
            },
        )
        assert payload(planned)["recipe_id"] == recipe_id

        regular = await client.call_tool(
            "add_regular_item",
            {"data": {"name": "Milk"}, "interval_days": 7},
        )
        item_id = payload(regular)["id"]
        await client.call_tool(
            "record_purchase", {"item_id": item_id, "purchased_on": "2026-01-01"}
        )
        suggestions = await client.call_tool(
            "get_shopping_list", {"start": "2026-01-05", "end": "2026-01-05"}
        )
        assert [item["id"] for item in payload(suggestions)["suggestions"]] == [item_id]

        confirmed = await client.call_tool("confirm_shopping_suggestion", {"item_id": item_id})
        capture_id = payload(confirmed)["id"]
        captures = await client.call_tool("list_capture_items")
        assert [item["id"] for item in payload(captures)] == [capture_id]
        assert service.list_items()[0].name == "Milk"

        household = await client.call_tool("get_household")
        assert payload(household)["name"] == "Test home"


@pytest.mark.asyncio
async def test_tool_catalog_annotations_and_guidance(test_database_url):
    mcp = build_mcp(KitchenService(test_database_url), Household)
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert {
        "list_recipes",
        "get_recipe",
        "save_recipe",
        "update_recipe",
        "list_meals",
        "plan_meal",
        "remove_meal",
        "add_shopping_item",
        "list_capture_items",
        "set_item_completed",
        "get_shopping_list",
        "list_regular_items",
        "add_regular_item",
        "confirm_shopping_suggestion",
        "record_purchase",
        "get_household",
    } <= tools.keys()
    assert "checkout" not in tools
    assert tools["list_recipes"].annotations.readOnlyHint is True
    assert tools["confirm_shopping_suggestion"].annotations.readOnlyHint is False
    assert tools["remove_meal"].annotations.destructiveHint is True
    assert "untrusted" in mcp.instructions.casefold()
    assert "after the user" in tools["record_purchase"].description
