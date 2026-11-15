import pytest
from fastmcp import Client
from test_api import make_client

from home_kitchen.basket.repository import BasketService
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.features import KitchenFeatures
from home_kitchen.mcp_server import build_mcp
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings


def test_catalogue_api_is_source_attributed_and_does_not_seed_personal_data(test_database_url):
    with make_client(test_database_url) as client:
        response = client.get("/api/catalogue/foods", params={"q": "laktosefri"})
        assert response.status_code == 200
        foods = response.json()
        assert len(foods) == 2
        assert len({food["id"] for food in foods}) == 2
        assert all(food["source"] == "matvaretabellen" for food in foods)
        assert all(
            food["source_attribution"]
            == "Matvaretabellen 2026. Mattilsynet. www.matvaretabellen.no"
            for food in foods
        )
        assert (
            client.get("/api/catalogue/foods", params={"q": "melk", "limit": 0}).status_code == 422
        )
        assert client.get("/api/recipes").json() == []
        assert client.get("/api/items").json() == []


@pytest.mark.asyncio
async def test_catalogue_mcp_uses_same_generic_records(test_database_url):
    service = KitchenService(test_database_url)
    features = KitchenFeatures(
        service, Settings(local_dev=True, database_url=test_database_url), Household
    )
    mcp = build_mcp(service, Household)
    install_feature_tools(mcp, features, BasketService(service, features.history))
    async with Client(mcp) as client:
        result = await client.call_tool("find_foods", {"query": "laktosefri"})
        assert result.structured_content["result"] == [
            food.model_dump(mode="json") for food in features.catalogue.autocomplete("laktosefri")
        ]


def test_exact_sku_memory_and_explicit_preferences_share_persistent_household_scope(
    test_database_url,
):
    from datetime import UTC, datetime

    with make_client(test_database_url) as client:
        food = client.get("/api/catalogue/foods", params={"q": "naturell yoghurt"}).json()[0]
        snapshot = {
            "provider": "oda",
            "product_id": "123",
            "name": "Exact yoghurt",
            "observed_at": datetime.now(UTC).isoformat(),
            "evidence_status": "provider_verified",
        }
        option = client.post(f"/api/catalogue/foods/{food['id']}/products", json=snapshot)
        assert option.status_code == 201
        data = option.json()
        assert data["preferred"] is False
        assert data["snapshot"]["evidence_status"] == "unverified"
        assert (
            client.put(f"/api/catalogue/products/{data['id']}/preferred").json()["preferred"]
            is True
        )
        assert client.get("/api/interactions").json() == []
    with make_client(test_database_url) as restarted:
        options = restarted.get(f"/api/catalogue/foods/{food['id']}/products").json()
        assert options[0]["id"] == data["id"]
        assert options[0]["preferred"] is True
        assert (
            restarted.delete(f"/api/catalogue/products/{data['id']}/preferred").json()["preferred"]
            is False
        )
        assert restarted.get("/api/items").json() == []
