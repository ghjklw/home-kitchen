from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from home_kitchen.app import create_app
from home_kitchen.settings import Settings


def app_for(database_url):
    return create_app(Settings(local_dev=True, database_url=database_url))


def test_shared_recipe_providers_history_retrieval_and_images(test_database_url):
    app = app_for(test_database_url)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert {entry["id"] for entry in client.get("/api/recipe-providers").json()} == {
            "manual",
            "web",
            "ai",
        }
        source = {
            "title": "Soup",
            "servings": 2,
            "ingredients": [{"name": "Tomato", "quantity": "4", "unit": "item"}],
            "instructions": ["Cook the tomatoes."],
            "image_url": "https://images.example.org/soup.jpg",
        }
        draft = client.post("/api/recipe-drafts/manual", json={"recipe": source}).json()
        assert draft["requires_review"] is True
        assert client.get("/api/recipes").json() == []
        recipe = client.post("/api/recipes", json=draft["recipe"]).json()
        assert recipe["image_url"] == source["image_url"]
        entry = client.post(
            "/api/interactions",
            json={
                "kind": "preference",
                "message": "Avoid coriander in our meals",
                "structured": {"ingredient": "coriander", "preference": "avoid"},
            },
        ).json()
        assert entry["actor_id"] == "local-development"
        assert entry["id"] in {
            item["id"] for item in client.get("/api/knowledge/search?query=coriander").json()
        }
        # Explicit preferences remain context even if the current query has no
        # matching words. This checks retrieval, not a mocked LLM's opinion.
        assert entry["id"] in {item.evidence_id for item in app.state.features.evidence("pasta")}
        assert client.get("/api/recipes").headers["cache-control"] == "no-store"


def test_basket_separates_needs_from_exact_product_choices(test_database_url):
    app = app_for(test_database_url)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        client.post("/api/items", json={"name": "Yoghurt", "quantity": "16", "unit": "item"})
        period = {"start": "2026-11-16", "end": "2026-11-22"}
        initial = client.get("/api/basket", params=period).json()
        need = initial["needs"][0]["need"]
        assert need["quantity"] == "16"
        for product_id, name in [
            ("fixture-tine", "Tine season"),
            ("fixture-yoplait", "Kids yoghurt"),
        ]:
            product = {
                "provider": "oda",
                "product_id": product_id,
                "name": name,
                "url": f"https://oda.com/no/products/{product_id}/",
                "image_url": f"https://images.example.org/{product_id}.jpg",
                "pack_quantity": "1",
                "pack_unit": "item",
                "price": "12.50",
                "currency": "NOK",
                "available": True,
                "observed_at": datetime.now(UTC).isoformat(),
                "evidence_status": "provider_verified",
            }
            response = client.put(
                "/api/basket/selections",
                json={
                    **period,
                    "selection": {
                        "need_id": need["id"],
                        "product": product,
                        "packages": 8,
                        "alternatives": [product],
                    },
                },
            )
            assert response.status_code == 200
        review = response.json()
        status = review["needs"][0]
        assert status["allocated_quantity"] == "16"
        assert status["remaining_quantity"] == "0"
        assert len(status["selections"]) == 2
        assert status["selections"][0]["product"]["image_url"]
        assert status["selections"][0]["alternatives"][0]["product_id"]
        # No caller, including an assistant, can certify its own product data.
        assert {selection["product"]["evidence_status"] for selection in status["selections"]} == {
            "unverified",
        }
        assert review["ready_for_review"] is False


@pytest.mark.asyncio
async def test_mcp_feedback_and_api_share_household_history(test_database_url):
    app = app_for(test_database_url)
    async with Client(app.state.mcp) as mcp:
        recorded = await mcp.call_tool(
            "record_household_interaction",
            {
                "data": {
                    "kind": "feedback",
                    "message": "The lentil dinner was a favourite",
                    "structured": {"liked": True},
                    "session_id": "fixture-chat",
                }
            },
        )
        entry = recorded.structured_content
        entry = entry.get("result", entry)
        assert entry["channel"] == "mcp"
        found = await mcp.call_tool("search_household_knowledge", {"query": "lentil"})
        results = found.structured_content
        results = results.get("result", results)
        assert results[0]["id"] == entry["id"]
    # Separate transports use the same persisted history and evidence IDs.
    with TestClient(app_for(test_database_url), client=("127.0.0.1", 5000)) as client:
        entries = client.get("/api/interactions").json()
        assert entries[0]["id"] == entry["id"]
        assert entries[0]["channel"] == "mcp"
