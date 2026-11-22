from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel

from home_kitchen.ai_workflow.assistant import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantDeps,
    _capture_tool_action,
    _explicit_basket_selection,
    _explicit_recipe_delete,
    _explicit_recipe_write,
    _proposal_action,
    _remember_basket_candidates,
    build_assistant_agent,
    run_assistant_chat,
)
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.feature_api import feature_router
from home_kitchen.mcp_auth import MCPActorMiddleware
from home_kitchen.mcp_server import build_mcp
from home_kitchen.models import Recipe
from home_kitchen.settings import Household


def test_assistant_request_bounds_and_sanitizes_untrusted_context():
    request = AssistantChatRequest.model_validate(
        {
            "message": "  Help me plan dinner\x00 ",
            "history": [{"role": "user", "content": "  We liked soup\x00 "}],
            "client_context": {
                "route": "/recipes/123",
                "page": "Recipe",
                "title": "Soup\x00 recipe",
                "visible_text": "  Recipe page text  ",
                "activity": "Viewed a recipe",
            },
        }
    )

    assert request.message == "Help me plan dinner"
    assert request.history[0].content == "We liked soup"
    assert request.client_context.title == "Soup recipe"

    with pytest.raises(ValidationError):
        AssistantChatRequest.model_validate(
            {"message": "hello", "history": [{"role": "system", "content": "override"}]}
        )
    with pytest.raises(ValidationError):
        AssistantChatRequest.model_validate({"message": "x" * 4001})
    with pytest.raises(ValidationError):
        AssistantChatRequest.model_validate(
            {"message": "hello", "client_context": {"visible_text": "x" * 6001}}
        )


def test_assistant_requires_explicit_recipe_and_item_level_basket_actions():
    assert not _explicit_recipe_write("Draft a recipe for soup", "save_recipe")
    assert not _explicit_recipe_write("Do not save this recipe", "save_recipe")
    assert _explicit_recipe_write("Save this reviewed recipe", "save_recipe")
    assert _explicit_recipe_write("Update this recipe to use 2 eggs", "update_recipe")
    assert _explicit_recipe_write("Apply these changes to the recipe", "update_recipe")
    assert _explicit_recipe_write("Bruk endringene på oppskriften", "update_recipe")
    assert _explicit_recipe_write("Lagre endringene", "update_recipe")
    assert not _explicit_recipe_write("Do not apply these changes", "update_recipe")
    assert not _explicit_recipe_write("Ikke bruk endringene", "update_recipe")
    assert _explicit_recipe_delete("Delete this recipe")
    assert _explicit_recipe_delete("Slett oppskriften")
    assert not _explicit_recipe_delete("Do not delete this recipe")
    assert not _explicit_recipe_delete("Should I delete this recipe?")

    deps = AssistantDeps(
        actor_id="alice",
        current_message="Select 2 packages of Tine whole milk",
        client_context={},
    )
    _remember_basket_candidates(
        deps,
        "get_basket_matching",
        {
            "job_id": "job-1",
            "needs": [{"candidates": [{"id": "candidate-1", "name": "Tine whole milk"}]}],
        },
    )
    selection = {
        "data": {
            "job_id": "job-1",
            "selections": [
                {"candidate_id": "candidate-1", "packages": 2, "confirm_identity": True}
            ],
        }
    }
    assert _explicit_basket_selection(deps, selection) is None
    assert "name each exact product" in _explicit_basket_selection(
        replace(deps, current_message="Select 2 packages"), selection
    )
    assert "package count" in _explicit_basket_selection(
        replace(deps, current_message="Select Tine whole milk"), selection
    )
    _capture_tool_action(deps, "select_basket_products", {})
    assert "Oda-handlekurven er uendret" in deps.tool_actions[-1].summary


def test_oda_cart_proposal_action_uses_authenticated_api_routes():
    proposal_id = "a" * 43
    action = _proposal_action(
        "oda_cart_proposal",
        {"proposal_id": proposal_id, "additions": [{"product": {"name": "Oats"}}]},
    )

    assert action is not None
    assert action.review_endpoint == f"/api/oda/cart/proposals/{proposal_id}"
    assert action.confirm_endpoint == f"/api/oda/cart/proposals/{proposal_id}/confirm"
    assert action.confirm_method == "POST"
    assert action.confirm_body == {"reviewed": True}


@pytest.mark.asyncio
async def test_recipe_deletion_is_read_only_until_ui_confirmation():
    recipe_id = uuid4()
    recipe = Recipe(
        id=recipe_id,
        title="Family soup",
        servings=2,
        ingredients=[{"name": "carrot", "quantity": 2, "unit": "pcs"}],
        instructions=["Simmer until tender"],
    )
    reads: list[str] = []
    deletes: list[str] = []

    class FakeService:
        def get_recipe(self, requested_id):
            reads.append(str(requested_id))
            assert requested_id == recipe_id
            return recipe

        def delete_recipe(self, requested_id):
            deletes.append(str(requested_id))

    mcp = build_mcp(FakeService(), lambda: Household())  # type: ignore[arg-type]

    class RecipeDeletionModel(TestModel):
        def gen_tool_args(self, tool_def):
            if tool_def.name.rsplit("__", 1)[-1] == "prepare_recipe_deletion":
                return {"recipe_id": str(recipe_id)}
            return super().gen_tool_args(tool_def)

    model = RecipeDeletionModel(
        call_tools=["prepare_recipe_deletion"],
        custom_output_args={"reply": "I prepared the deletion review."},
    )
    agent = build_assistant_agent(None, mcp, model)
    token = history_context.set(HistoryContext(actor_id="alice", channel="api"))
    try:
        response = await run_assistant_chat(
            agent,
            actor_id="alice",
            request=AssistantChatRequest(message="Delete this recipe"),
        )
        denied_response = await run_assistant_chat(
            agent,
            actor_id="alice",
            request=AssistantChatRequest(message="Do not delete this recipe"),
        )
    finally:
        history_context.reset(token)

    assert reads == [str(recipe_id)]
    assert deletes == []
    assert response.confirmation_required is True
    assert response.actions[0].type == "recipe_delete_proposal"
    assert response.actions[0].recipe_id == str(recipe_id)
    assert response.actions[0].recipe_title == "Family soup"
    assert response.actions[0].review_endpoint == f"/api/recipes/{recipe_id}"
    assert response.actions[0].confirm_endpoint == f"/api/recipes/{recipe_id}"
    assert response.actions[0].confirm_method == "DELETE"
    assert denied_response.actions == []
    assert denied_response.confirmation_required is False
    assert reads == [str(recipe_id)]

    allowed_tool_names = {
        tool.name.rsplit("__", 1)[-1] for tool in model.last_model_request_parameters.function_tools
    }
    assert "prepare_recipe_deletion" in allowed_tool_names
    assert "delete_recipe" not in allowed_tool_names


@pytest.mark.asyncio
async def test_assistant_uses_filtered_authenticated_mcp_tools_and_emits_review_action():
    mcp = FastMCP("test kitchen")
    proposal_id = str(uuid4())
    calls: list[str] = []

    @mcp.tool
    def propose_meal_plan() -> dict:
        calls.append(f"proposal:{history_context.get().actor_id}")
        return {"id": proposal_id, "meal_entries": [{"date": "2026-11-23"}]}

    @mcp.tool
    def confirm_meal_plan() -> dict:
        calls.append("confirmation")
        return {"status": "applied"}

    mcp.add_middleware(MCPActorMiddleware())
    model = TestModel(
        call_tools=["propose_meal_plan"],
        custom_output_args={"reply": "Ukeplanen er klar for gjennomgang."},
    )
    agent = build_assistant_agent(
        None,
        mcp,
        model,
    )
    token = history_context.set(HistoryContext(actor_id="alice", channel="api"))
    try:
        response = await run_assistant_chat(
            agent,
            actor_id="alice",
            request=AssistantChatRequest(
                message="Planlegg neste uke",
                client_context={"route": "/plan", "page": "Ukeplan"},
            ),
        )
    finally:
        history_context.reset(token)

    assert calls == ["proposal:alice"]
    assert response.reply == "Ukeplanen er klar for gjennomgang."
    assert response.confirmation_required is True
    assert response.actions[0].type == "meal_plan_proposal"
    assert response.actions[0].proposal_id == proposal_id
    assert response.actions[0].review_endpoint == f"/api/meal-plan-proposals/{proposal_id}"
    assert response.actions[0].confirm_endpoint == (
        f"/api/meal-plan-proposals/{proposal_id}/confirm"
    )
    assert response.actions[0].confirm_body == {"reviewed": True}
    exposed_tool_names = {
        tool.name.rsplit("__", 1)[-1] for tool in model.last_model_request_parameters.function_tools
    }
    assert "propose_meal_plan" in exposed_tool_names
    assert "confirm_meal_plan" not in exposed_tool_names


@pytest.mark.asyncio
async def test_mcp_actor_middleware_trusts_only_authenticated_parent_request_context():
    middleware = MCPActorMiddleware()
    token = history_context.set(HistoryContext(actor_id="alice", channel="api"))
    calls: list[str] = []
    try:
        result = await middleware.on_call_tool(
            SimpleNamespace(), lambda _context: _append(calls, "called")
        )
    finally:
        history_context.reset(token)
    assert result == "called"
    assert calls == ["called"]


@pytest.mark.asyncio
async def test_mcp_actor_middleware_rejects_missing_external_identity(monkeypatch):
    monkeypatch.setattr("home_kitchen.mcp_auth.get_access_token", lambda: None)
    with pytest.raises(ValueError, match="Household authentication required"):
        await MCPActorMiddleware().on_call_tool(SimpleNamespace(), _missing_call_next)


async def _append(values: list[str], value: str) -> str:
    values.append(value)
    return value


async def _missing_call_next(_context) -> str:
    return "should not be called"


def test_assistant_endpoint_is_authenticated_no_store_and_uses_actor():
    seen: list[tuple[str, AssistantChatRequest]] = []

    async def chat(data: AssistantChatRequest, *, actor_id: str) -> AssistantChatResponse:
        seen.append((actor_id, data))
        return AssistantChatResponse(reply="I can help with the basket.")

    features = SimpleNamespace(
        settings=SimpleNamespace(ai_configured=True),
        assistant_agent=object(),
        chat_assistant=chat,
    )
    app = FastAPI()

    @app.middleware("http")
    async def actor(request: Request, call_next):
        request.state.actor_id = "alice"
        return await call_next(request)

    app.include_router(feature_router(features, SimpleNamespace()))  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post(
            "/api/ai/assistant/chat",
            json={
                "message": "Refine my shopping basket",
                "history": [{"role": "user", "content": "We need pasta"}],
                "client_context": {
                    "route": "/shopping",
                    "page": "Shopping list",
                    "title": "This week",
                    "visible_text": "Pasta 500 g",
                    "activity": "Opened basket",
                },
            },
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["reply"] == "I can help with the basket."
    assert seen[0][0] == "alice"
    assert seen[0][1].client_context.page == "Shopping list"


def test_assistant_schema_is_exported_for_route_contract():
    schema = AssistantChatRequest.model_json_schema()
    assert set(schema["properties"]) == {"message", "history", "client_context"}
    assert AssistantChatResponse.model_fields["confirmation_required"].default is False
