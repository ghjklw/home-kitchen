import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from home_kitchen.app import create_app
from home_kitchen.settings import Settings


def recipe_payload():
    return {
        "title": "Tomato pasta",
        "servings": 2,
        "ingredients": [{"name": "Pasta", "quantity": 200, "unit": "g"}],
        "instructions": ["Boil the pasta"],
    }


async def test_api_and_mcp_record_typed_feedback_and_recommendation_evidence(test_database_url):
    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        recipe = client.post("/api/recipes", json=recipe_payload()).json()
        recipe_id = recipe["id"]

        rating = client.post(
            f"/api/recipes/{recipe_id}/feedback",
            json={"rating": 4},
        )
        assert rating.status_code == 201
        rating_entry = rating.json()
        assert rating_entry["kind"] == "recipe_rating"
        assert rating_entry["channel"] == "api"
        assert rating_entry["recipe_id"] == recipe_id
        assert rating_entry["structured"] == {"rating": 4}
        assert rating_entry["message"] == 'Feedback for recipe "Tomato pasta". Rating: 4/5.'

    async with Client(app.state.mcp) as mcp:
        result = await mcp.call_tool(
            "record_recipe_feedback",
            {
                "recipe_id": recipe_id,
                "data": {"would_make_again": False, "notes": "Too salty"},
            },
        )
    mcp_entry = result.structured_content
    mcp_entry = mcp_entry.get("result", mcp_entry)
    assert mcp_entry["kind"] == "recipe_rating"
    assert mcp_entry["channel"] == "mcp"
    assert mcp_entry["recipe_id"] == recipe_id
    assert mcp_entry["structured"] == {"would_make_again": False, "notes": "Too salty"}
    assert 'Feedback for recipe "Tomato pasta".' in mcp_entry["message"]
    assert "Would make again: no." in mcp_entry["message"]

    evidence = app.state.features.evidence("unrelated dinner request")
    matching = [item for item in evidence if item.evidence_id == mcp_entry["id"]]
    assert len(matching) == 1
    evidence_document = json.loads(matching[0].text)
    assert "Tomato pasta" in evidence_document["message"]
    assert json.loads(evidence_document["structured"]) == {
        "notes": "Too salty",
        "would_make_again": False,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"rating": 0},
        {"rating": 6},
        {"rating": True},
        {"notes": "   "},
    ],
)
def test_feedback_rejects_invalid_or_empty_values(test_database_url, payload):
    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        recipe = client.post("/api/recipes", json=recipe_payload()).json()
        response = client.post(f"/api/recipes/{recipe['id']}/feedback", json=payload)
    assert response.status_code == 422


def test_feedback_requires_existing_recipe(test_database_url):
    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    unknown_recipe_id = UUID("00000000-0000-0000-0000-000000000001")
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post(
            f"/api/recipes/{unknown_recipe_id}/feedback",
            json={"rating": 3},
        )
    assert response.status_code == 404
