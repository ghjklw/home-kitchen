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


def test_inventory_and_in_store_progress_reduce_purchases_without_changing_needs(test_database_url):
    app = app_for(test_database_url)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        period = {"start": "2026-11-16", "end": "2026-11-22"}
        client.post(
            "/api/items",
            json={"name": "Yoghurts", "quantity": "16", "category": "Dairy"},
        ).raise_for_status()
        client.post(
            "/api/items", json={"name": "Apples", "quantity": "2", "category": "Produce"}
        ).raise_for_status()
        stock = client.put(
            "/api/inventory",
            json={"name": "Yoghurts", "quantity": "4", "category": "Dairy"},
        )
        assert stock.status_code == 200
        plan = client.get("/api/purchase-plan", params=period).json()
        yoghurt = next(line for line in plan["lines"] if line["name"] == "Yoghurts")
        assert yoghurt["required_quantity"] == "16"
        assert yoghurt["covered_quantity"] == "4"
        assert yoghurt["to_buy_quantity"] == "12"
        basket = client.get("/api/basket", params=period).json()
        assert (
            next(line for line in basket["needs"] if line["need"]["name"] == "Yoghurts")["need"][
                "quantity"
            ]
            == "12"
        )
        trip = client.get("/api/store-trip", params=period).json()
        assert [line["category"] for line in trip["lines"]] == ["Dairy", "Produce"]
        checked = client.put(
            f"/api/store-trip/items/{yoghurt['id']}", params=period, json={"checked": True}
        ).json()
        assert checked["checked_count"] == 1
        assert all(not item["completed"] for item in client.get("/api/items").json())
        assert client.get("/api/inventory").json()[0]["quantity"] == "4"
        assert not any(
            entry["kind"] == "purchase" for entry in client.get("/api/interactions").json()
        )
        # More needed later must not appear fully picked based on an older check.
        client.post("/api/items", json={"name": "Yoghurts", "quantity": "2"}).raise_for_status()
        changed = client.get("/api/store-trip", params=period).json()
        yoghurt_after = next(line for line in changed["lines"] if line["id"] == yoghurt["id"])
        assert yoghurt_after["to_buy_quantity"] == "14"
        assert yoghurt_after["checked"] is False
        # Fully covered requirements remain visible in planning and disappear from buying.
        client.put("/api/inventory", json={"name": "Yoghurts", "quantity": "18"}).raise_for_status()
        covered = client.get("/api/purchase-plan", params=period).json()
        assert (
            next(line for line in covered["lines"] if line["id"] == yoghurt["id"])[
                "to_buy_quantity"
            ]
            == "0"
        )
        assert len(client.get("/api/store-trip", params=period).json()["lines"]) == 1
        assert len(client.get("/api/basket", params=period).json()["needs"]) == 1
