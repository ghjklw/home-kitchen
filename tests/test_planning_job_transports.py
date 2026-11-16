from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from test_api import make_client, recipe_payload

from home_kitchen.app import create_app
from home_kitchen.basket.jobs import BasketMatchJob
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.settings import Settings


def test_plan_proposal_is_atomic_explicit_and_additive(test_database_url):
    with make_client(test_database_url) as client:
        recipe = client.post("/api/recipes", json=recipe_payload()).json()
        meals = [
            {"date": "2026-12-08", "slot": slot, "recipe_id": recipe["id"], "servings": 5}
            for slot in ["lunch", "dinner"]
        ]
        proposal = client.post("/api/meal-plan-proposals", json={"meals": meals})
        assert proposal.status_code == 201
        assert proposal.headers["cache-control"] == "no-store"
        period = {"start": "2026-12-08", "end": "2026-12-08"}
        assert client.get("/api/meals", params=period).json() == []
        path = "/api/meal-plan-proposals/" + proposal.json()["id"] + "/confirm"
        assert client.post(path, json={}).status_code == 422
        applied = client.post(path, json={"reviewed": True})
        assert applied.status_code == 200
        assert len(applied.json()["applied_meal_ids"]) == 2
        assert client.post(path, json={"reviewed": True}).json() == applied.json()
        repeated = client.post("/api/meal-plan-proposals", json={"meals": meals})
        assert repeated.status_code == 201
        repeated_path = "/api/meal-plan-proposals/" + repeated.json()["id"] + "/confirm"
        assert client.post(repeated_path, json={"reviewed": True}).status_code == 200
        assert len(client.get("/api/meals", params=period).json()) == 4


def test_background_job_rest_passes_actor_and_exact_retry_scope(test_database_url):
    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    value = BasketMatchJob(job_id="job", start=date(2026, 12, 1), end=date(2026, 12, 7), needs=[])
    app.state.features.basket_jobs.start = AsyncMock(return_value=value)
    app.state.features.basket_jobs.get = AsyncMock(return_value=value)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post(
            "/api/basket/match-jobs",
            json={"start": "2026-12-01", "end": "2026-12-07", "force_refresh": True},
        )
        assert response.status_code == 202
        assert response.headers["cache-control"] == "no-store"
        assert app.state.features.basket_jobs.start.call_args.args[0] == "local-development"
        assert app.state.features.basket_jobs.start.call_args.kwargs["force_refresh"] is True
        assert client.get("/api/basket/match-jobs/job").status_code == 200


@pytest.mark.asyncio
async def test_mcp_meal_proposal_requires_separate_explicit_confirmation(test_database_url):
    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    app.state.features.basket_jobs.start = AsyncMock(
        return_value=BasketMatchJob(
            job_id="test-job", start=date(2026, 12, 1), end=date(2026, 12, 7), needs=[]
        )
    )
    token = history_context.set(HistoryContext(actor_id="person", channel="mcp"))
    try:
        async with Client(app.state.mcp) as client:
            job = await client.call_tool(
                "start_basket_matching",
                {"start": "2026-12-01", "end": "2026-12-07", "force_refresh": True},
            )
            assert job.structured_content["job_id"] == "test-job"
            assert app.state.features.basket_jobs.start.call_args.args[0] == "person"
            assert app.state.features.basket_jobs.start.call_args.kwargs["need_ids"] is None
            saved = await client.call_tool("save_recipe", {"data": recipe_payload()})
            recipe_id = saved.structured_content["id"]
            result = await client.call_tool(
                "propose_meal_plan",
                {"meals": [{"date": "2026-12-09", "recipe_id": recipe_id, "servings": 5}]},
            )
            proposal = result.structured_content
            assert app.state.service.list_meals(date(2026, 12, 9), date(2026, 12, 9)) == []
            confirmed = await client.call_tool(
                "confirm_meal_plan", {"proposal_id": proposal["id"], "reviewed": True}
            )
            assert confirmed.structured_content["status"] == "applied"
    finally:
        history_context.reset(token)
