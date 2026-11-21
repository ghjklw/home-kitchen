"""Wire-level MCP contracts for clients that consume only text content."""

import json
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from test_mcp_oauth import broker_token, configure_offline_verifier, oauth_settings

from home_kitchen import imports
from home_kitchen.app import create_app
from home_kitchen.basket.repository import BasketService
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.features import KitchenFeatures
from home_kitchen.imports import ImportedDraft
from home_kitchen.mcp_server import build_mcp
from home_kitchen.models import Ingredient, MealCreate, RecipeCreate, RecipeType
from home_kitchen.recipes.discovery import MarmitonDiscoveryProvider, RecipeDiscoveryRegistry
from home_kitchen.recipes.models import DraftIngredient, EditableRecipeDraft, SourceIngredient
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings


def _wire_call(client, request_id, name, arguments, *, path="/", headers=None):
    request_headers = {
        "MCP-Protocol-Version": "2025-11-25",
        "Accept": "application/json, text/event-stream",
    }
    if headers:
        request_headers.update(headers)
    response = client.post(
        path,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=request_headers,
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


def test_all_mcp_tools_expose_valid_wire_schemas_and_text_json_contracts():
    """Verify every MCP tool exposes annotations, schemas, and JSON text wire output."""
    service = MagicMock()
    features = MagicMock()
    basket = MagicMock()
    missing_id = uuid4()

    def remove_side_effect(item_id):
        if item_id == missing_id:
            raise KeyError(str(item_id))

    features.inventory.remove.side_effect = remove_side_effect
    mcp = build_mcp(service, Household)
    install_feature_tools(mcp, features, basket)
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
                    "clientInfo": {"name": "gemini-compat-check", "version": "1"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert initialized.status_code == 200, initialized.text
        assert _payload(initialized)["result"]["protocolVersion"] == "2025-11-25"

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
        assert len(tools) >= 87
        for name, tool in tools.items():
            assert tool.get("description"), f"{name} missing description"
            annotations = tool.get("annotations")
            assert isinstance(annotations, dict), f"{name} missing annotations"
            assert isinstance(annotations.get("readOnlyHint"), bool), f"{name} missing readOnlyHint"
            assert tool.get("inputSchema", {}).get("type") == "object", (
                f"{name} inputSchema must be object"
            )
            assert tool.get("outputSchema", {}).get("type") == "object", (
                f"{name} outputSchema must be object"
            )

        deleted = _wire_call(
            client,
            3,
            "remove_inventory",
            {"item_id": str(uuid4())},
        )
        assert deleted["isError"] is False
        assert all(item["type"] == "text" for item in deleted["content"])
        assert json.loads(deleted["content"][0]["text"]) == {"deleted": True}
        assert deleted["structuredContent"] == {"deleted": True}

        missing = _wire_call(
            client,
            4,
            "remove_inventory",
            {"item_id": str(missing_id)},
        )
        assert missing["isError"] is True
        assert "Inventory item not found" in missing["content"][0]["text"]


@pytest.mark.mocked_provider
@pytest.mark.asyncio
async def test_full_recipe_to_shopping_journey_through_mcp_with_oauth(
    test_database_url, monkeypatch
):
    """Exercise discovery -> draft -> plan -> shop -> purchase -> cook -> feedback over OAuth MCP."""
    import time

    recipe_url = "https://www.marmiton.org/recettes/recette_mcp-tomatpasta_202.aspx"
    search_html = (
        '<ul class="search-list"><li class="search-list__item">'
        '<a class="card-content__title" href="/recettes/recette_mcp-tomatpasta_202.aspx">'
        "Kremet tomatpasta</a>"
        "</li></ul>"
    )
    discovery_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=search_html,
            )
        ),
        trust_env=False,
    )

    async def fake_import(url: str) -> ImportedDraft:
        assert url == recipe_url
        return ImportedDraft(
            title="Pasta à la tomate",
            servings=2,
            ingredients=["200 g pasta", "100 ml fløte"],
            instructions=["Kok pasta etter anvisning.", "Vend inn saus."],
            source_url=recipe_url,
            recipe_type=RecipeType.MIDDAG,
            prep_time_minutes=20,
        )

    monkeypatch.setattr(imports, "import_recipe", fake_import)

    adapted_output = EditableRecipeDraft(
        title="Kremet tomatpasta (MCP)",
        servings=2,
        ingredients=[
            DraftIngredient(name="Pasta", quantity=Decimal("200"), unit="g", source_indices=[0]),
            DraftIngredient(name="Fløte", quantity=Decimal("100"), unit="ml", source_indices=[1]),
        ],
        instructions=["Kok pasta i lettsaltet vann.", "Rør inn fløte og server."],
        source_url=recipe_url,
        source_ingredients=[
            SourceIngredient(index=0, text="200 g pasta"),
            SourceIngredient(index=1, text="100 ml fløte"),
        ],
        recipe_type=RecipeType.MIDDAG,
        prep_time_minutes=20,
    )
    ai_agent = Agent(TestModel(custom_output_args=adapted_output), output_type=EditableRecipeDraft)

    settings = oauth_settings(test_database_url)
    app = create_app(settings, ai=ai_agent)
    provider = app.state.mcp.auth
    upstream_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    configure_offline_verifier(provider, upstream_key)
    token = await broker_token(provider, upstream_key)

    app.state.features.recipe_discovery = RecipeDiscoveryRegistry(
        [MarmitonDiscoveryProvider(http_client=discovery_client)],
        context_provider=app.state.features.discovery_context,
    )

    async def fake_oda_search(actor_id: str, queries: list[str], page: int, size: int) -> dict:
        assert actor_id == "household-member"
        groups = []
        for query in queries:
            if "pasta" in query.casefold():
                products = [
                    {
                        "id": 1001,
                        "name": "Barilla Spaghetti Pasta 500 g",
                        "description": "500 g",
                        "price": "24.90",
                        "unitPrice": "49.80",
                        "unitName": "kg",
                        "brand": "Barilla",
                        "url": "https://oda.com/no/products/1001-barilla-spaghetti/",
                        "availability": {
                            "isAvailable": True,
                            "description": "På lager",
                            "code": "available",
                        },
                    }
                ]
            else:
                products = []
            groups.append({"query": query, "hasMore": False, "products": products})
        return {"result": groups}

    app.state.features.oda.search_products = AsyncMock(side_effect=fake_oda_search)

    auth_headers = {"Authorization": f"Bearer {token}"}
    plan_start = "2026-11-23"
    plan_end = "2026-11-29"
    meal_date = "2026-11-25"

    def call_mcp(client, req_id: int, name: str, arguments: dict):
        result = _wire_call(client, req_id, name, arguments, path="/mcp/", headers=auth_headers)
        assert result["isError"] is False, result
        decoded = json.loads(result["content"][0]["text"])
        structured = result["structuredContent"]
        assert decoded == structured.get("result", structured)
        return decoded

    try:
        with TestClient(
            app, base_url="https://kitchen.example", client=("203.0.113.20", 5000)
        ) as client:
            # Unauthenticated MCP call is rejected separately from protocol implementation.
            denied = client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert denied.status_code == 401
            assert "resource_metadata=" in denied.headers["www-authenticate"]

            # 1. Discovery
            discovered = call_mcp(
                client,
                1,
                "find_recipes",
                {"request": {"query": "tomatpasta", "sources": ["marmiton"], "limit": 5}},
            )
            assert len(discovered["results"]) == 1
            assert discovered["results"][0]["url"] == recipe_url
            assert call_mcp(client, 2, "list_recipes", {}) == []

            # 2. Draft & save
            draft = call_mcp(
                client,
                3,
                "draft_recipe",
                {"provider_id": "ai", "request": {"url": recipe_url}},
            )
            assert draft["requires_review"] is True
            assert call_mcp(client, 4, "list_recipes", {}) == []

            adapted = draft["adapted"]
            saved = call_mcp(
                client,
                5,
                "save_recipe",
                {
                    "data": {
                        "title": adapted["title"],
                        "servings": adapted["servings"],
                        "ingredients": [
                            {
                                "name": item["name"],
                                "quantity": item["quantity"],
                                "unit": item["unit"],
                                "source_indices": item["source_indices"],
                            }
                            for item in adapted["ingredients"]
                        ],
                        "instructions": adapted["instructions"],
                        "source": "ai",
                        "source_url": adapted["source_url"],
                        "source_ingredients": adapted["source_ingredients"],
                        "provenance": adapted["provenance"],
                        "recipe_type": adapted["recipe_type"],
                        "prep_time_minutes": adapted["prep_time_minutes"],
                    }
                },
            )
            recipe_id = saved["id"]

            # 3. Meal plan proposal & confirmation
            proposal = call_mcp(
                client,
                6,
                "propose_meal_plan",
                {"meals": [{"date": meal_date, "recipe_id": recipe_id, "servings": 5}]},
            )
            assert call_mcp(client, 7, "list_meals", {"start": plan_start, "end": plan_end}) == []
            confirmed_plan = call_mcp(
                client,
                8,
                "confirm_meal_plan",
                {"proposal_id": proposal["id"], "reviewed": True},
            )
            assert confirmed_plan["status"] == "applied"
            meal_id = confirmed_plan["applied_meal_ids"][0]

            # 4. Shopping list, inventory & purchase plan
            shopping = call_mcp(
                client, 9, "get_shopping_list", {"start": plan_start, "end": plan_end}
            )
            by_name = {line["name"]: line for line in shopping["lines"]}
            assert Decimal(by_name["Pasta"]["quantity"]) == Decimal("500")

            call_mcp(
                client,
                10,
                "set_inventory",
                {"data": {"name": "Pasta", "quantity": "100", "unit": "g", "category": "Tørrvare"}},
            )
            temp_stock = call_mcp(
                client,
                11,
                "set_inventory",
                {"data": {"name": "Salt", "quantity": "1", "unit": "item", "category": "Krydder"}},
            )
            assert call_mcp(client, 12, "remove_inventory", {"item_id": temp_stock["id"]}) == {
                "deleted": True
            }

            purchase_plan = call_mcp(
                client, 13, "get_purchase_plan", {"start": plan_start, "end": plan_end}
            )
            plan_lines = {line["name"]: line for line in purchase_plan["lines"]}
            assert Decimal(plan_lines["Pasta"]["to_buy_quantity"]) == Decimal("400")
            pasta_need_id = plan_lines["Pasta"]["id"]

            # 5. Store trip picking (does not record a purchase)
            trip = call_mcp(client, 14, "get_store_trip", {"start": plan_start, "end": plan_end})
            assert trip["checked_count"] == 0
            checked_trip = call_mcp(
                client,
                15,
                "check_store_trip_item",
                {"start": plan_start, "end": plan_end, "need_id": pasta_need_id, "checked": True},
            )
            assert checked_trip["checked_count"] == 1
            assert call_mcp(client, 16, "list_completed_store_trips", {}) == []

            # 6. Basket matching & batch product selection
            job = call_mcp(
                client,
                17,
                "start_basket_matching",
                {"start": plan_start, "end": plan_end},
            )
            for req_idx in range(18, 120):
                if job["status"] in {"complete", "failed"}:
                    break
                time.sleep(0.01)
                job = call_mcp(client, req_idx, "get_basket_matching", {"job_id": job["job_id"]})
            assert job["status"] == "complete"
            pasta_match = next(n for n in job["needs"] if n["need_id"] == pasta_need_id)
            assert pasta_match["candidates"]
            candidate_id = pasta_match["candidates"][0]["id"]

            basket_review = call_mcp(
                client,
                121,
                "select_basket_products",
                {
                    "data": {
                        "start": plan_start,
                        "end": plan_end,
                        "job_id": job["job_id"],
                        "selections": [
                            {
                                "need_id": pasta_need_id,
                                "candidate_id": candidate_id,
                                "packages": 1,
                                "confirm_identity": True,
                            }
                        ],
                    }
                },
            )
            pasta_basket = next(n for n in basket_review["needs"] if n["id"] == pasta_need_id)
            assert pasta_basket["selections"][0]["product"]["product_id"] == "1001"
            assert pasta_basket["selections"][0]["product"]["evidence_status"] == "unverified"

            # 7. Explicit purchase completion
            completion_review = call_mcp(
                client,
                122,
                "review_store_trip_completion",
                {"start": plan_start, "end": plan_end},
            )
            completion_payload = {
                "start": plan_start,
                "end": plan_end,
                "review_token": completion_review["review_token"],
                "reviewed": True,
                "idempotency_key": str(uuid4()),
                "purchased_on": date.today().isoformat(),
                "purchases": [
                    {
                        "need_id": pasta_need_id,
                        "product": {"provider": "oda", "product_id": "1001", "packages": 1},
                        "close_meal_ids": [meal_id],
                    }
                ],
            }
            completed = call_mcp(client, 123, "complete_store_trip", {"data": completion_payload})
            assert completed["actor_id"] == "household-member"
            assert len(completed["lines"]) == 1
            replayed = call_mcp(client, 124, "complete_store_trip", {"data": completion_payload})
            assert replayed == completed
            assert len(call_mcp(client, 125, "list_completed_store_trips", {})) == 1

            # 8. Cooking & feedback
            cooking = call_mcp(client, 126, "get_planned_meal_recipe", {"meal_id": meal_id})
            assert cooking["servings"] == 5
            assert cooking["source_recipe"]["servings"] == 2

            feedback = call_mcp(
                client,
                127,
                "record_recipe_feedback",
                {
                    "recipe_id": recipe_id,
                    "data": {"rating": 5, "would_make_again": True, "notes": "Flott via MCP"},
                },
            )
            assert feedback["channel"] == "mcp"
            assert feedback["actor_id"] == "household-member"

            stats = call_mcp(client, 128, "get_recipe_stats", {"recipe_id": recipe_id})
            assert stats["times_planned"] == 1
            assert stats["latest_rating"] == 5
            evidence = call_mcp(
                client, 129, "search_household_knowledge", {"query": "Flott via MCP"}
            )
            assert any(item["evidence_id"] == feedback["id"] for item in evidence)
    finally:
        await discovery_client.aclose()
