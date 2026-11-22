"""Offline unit and HTTP transport tests for the conversational week-planning chatbot."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from home_kitchen import ai as ai_module
from home_kitchen.ai_workflow.service import AiWorkflowService
from home_kitchen.app import create_app
from home_kitchen.catalogue.identity import IdentityCorrectionEvent
from home_kitchen.context import BoughtBeforeItem, PurchaseHistoryContext
from home_kitchen.features import KitchenFeatures
from home_kitchen.inventory import InventoryItem
from home_kitchen.models import Ingredient, Meal, MealCreate, Recipe, RecipeActivityStats
from home_kitchen.recipes import EvidenceSnippet
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings
from home_kitchen.week_chat import (
    WeekChatApplyRequest,
    WeekChatRequest,
    WeekChatTurn,
    WeekPlanChange,
    WeekPlanToolbox,
    apply_week_chat_changes,
    build_week_chat_agent,
    run_week_chat,
)

pytestmark = pytest.mark.mocked_provider


class InMemoryWeekPlanningBackend:
    """In-memory implementation of WeekPlanningBackendPort for deterministic offline tests."""

    def __init__(
        self,
        recipes: list[Recipe] | None = None,
        meals: list[Meal] | None = None,
        stats: dict[UUID, RecipeActivityStats] | None = None,
    ) -> None:
        self._recipes = list(recipes or [])
        self._meals: dict[UUID, Meal] = {meal.id: meal for meal in (meals or [])}
        self._stats = dict(stats or {})

    def list_recipes(self) -> list[Recipe]:
        return list(self._recipes)

    def list_meals(self, start: date, end: date) -> list[Meal]:
        return sorted(
            (meal for meal in self._meals.values() if start <= meal.date <= end),
            key=lambda meal: (meal.date, meal.scheduled_time or time.max, str(meal.id)),
        )

    def plan_meal(self, data: MealCreate) -> Meal:
        meal = Meal(id=uuid4(), **data.model_dump())
        self._meals[meal.id] = meal
        return meal

    def update_meal(self, meal_id: UUID, data: MealCreate) -> Meal:
        if meal_id not in self._meals:
            raise KeyError(meal_id)
        updated = Meal(id=meal_id, **data.model_dump())
        self._meals[meal_id] = updated
        return updated

    def delete_meal(self, meal_id: UUID) -> None:
        if meal_id not in self._meals:
            raise KeyError(meal_id)
        del self._meals[meal_id]

    def get_recipes_stats(
        self, recipe_ids: list[UUID] | None = None
    ) -> dict[UUID, RecipeActivityStats]:
        if recipe_ids is None:
            return dict(self._stats)
        return {rid: self._stats[rid] for rid in recipe_ids if rid in self._stats}


def _make_recipe(
    title: str,
    *,
    recipe_type: str | None = "middag",
    prep_time_minutes: int | None = 25,
    prep_category: str | None = "rask",
    ingredients: list[str] | None = None,
) -> Recipe:
    return Recipe(
        id=uuid4(),
        title=title,
        servings=4,
        recipe_type=recipe_type,  # type: ignore[arg-type]
        prep_time_minutes=prep_time_minutes,
        prep_category=prep_category,  # type: ignore[arg-type]
        ingredients=[
            Ingredient(name=name, quantity=Decimal("200"), unit="g")
            for name in (ingredients or ["laks", "pasta"])
        ],
        instructions=["Tilbered og server."],
    )


def _tool_turn_model(
    tool_calls: list[tuple[str, dict[str, Any]]],
    *,
    reply: str,
    notes: list[str] | None = None,
    captured: list[list[ModelMessage]] | None = None,
) -> FunctionModel:
    """Create a deterministic FunctionModel that executes tool_calls then returns reply."""

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if captured is not None:
            captured.append(list(messages))
        has_tool_return = any(
            getattr(part, "part_kind", None) == "tool-return"
            for msg in messages
            for part in msg.parts
        )
        if not has_tool_return and tool_calls:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=name,
                        args=args,
                        tool_call_id=f"call-{idx}",
                    )
                    for idx, (name, args) in enumerate(tool_calls)
                ]
            )
        output_tool = info.output_tools[0].name
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=output_tool,
                    args={"reply": reply, "notes": notes or []},
                    tool_call_id="output-0",
                )
            ]
        )

    return FunctionModel(model_fn)


def test_ai_module_exports_week_chat_symbols():
    for symbol in (
        "WeekChatApplyRequest",
        "WeekChatApplyResponse",
        "WeekChatRequest",
        "WeekChatResponse",
        "WeekChatTurn",
        "WeekPlanChange",
        "WeekPlanToolbox",
        "build_week_chat_agent",
        "run_week_chat",
    ):
        assert hasattr(ai_module, symbol)


def test_toolbox_add_replace_remove_apply_and_propose_modes():
    start = date(2026, 11, 23)
    soup = _make_recipe("Fiskesuppe", prep_time_minutes=20, prep_category="rask")
    taco = _make_recipe("Taco", prep_time_minutes=30, prep_category="rask")
    stew = _make_recipe("Langtidsgryte", prep_time_minutes=120, prep_category="langtid")
    existing_tue = Meal(
        id=uuid4(),
        date=start + timedelta(days=1),
        recipe_id=soup.id,
        servings=2,
        label="Middag",
    )
    existing_fri = Meal(
        id=uuid4(),
        date=start + timedelta(days=4),
        recipe_id=taco.id,
        servings=4,
        label="Fredagskos",
    )
    backend = InMemoryWeekPlanningBackend(
        recipes=[soup, taco, stew],
        meals=[existing_tue, existing_fri],
        stats={
            soup.id: RecipeActivityStats(
                recipe_id=soup.id,
                times_planned=3,
                average_rating=4.5,
                latest_rating=5,
                would_make_again=True,
                feedback_count=2,
            )
        },
    )

    # 1. Propose mode stages changes without mutating backend
    propose_box = WeekPlanToolbox(
        planning=backend,
        household=Household(default_servings=3, dietary_preferences=["lite sukker"]),
        recipes=[soup, taco, stew],
        existing_meals=[existing_tue, existing_fri],
        start=start,
        days=7,
        apply_changes=False,
        recipe_stats=backend.get_recipes_stats(),
        inventory_items=[
            InventoryItem(id=uuid4(), name="Laks", quantity=Decimal("400"), unit="g"),
            InventoryItem(id=uuid4(), name="Tomat", quantity=Decimal("0"), unit="stk"),
        ],
    )
    assert len(propose_box.get_available_ingredients()) == 1
    assert propose_box.get_available_ingredients()[0]["name"] == "Laks"
    quick_recipes = propose_box.list_available_recipes(max_prep_minutes=25, prep_category="rask")
    assert [r["title"] for r in quick_recipes] == ["Fiskesuppe"]
    assert quick_recipes[0]["stats"]["average_rating"] == 4.5

    added = propose_box.add_meal(meal_date=start, recipe_id=taco.id)
    replaced = propose_box.replace_meal(meal_id=existing_tue.id, recipe_id=stew.id, servings=4)
    removed = propose_box.remove_meal(meal_id=existing_fri.id)
    assert added.status == "proposed"
    assert added.meal_id is None
    assert added.servings == 3  # household.default_servings
    assert replaced.status == "proposed"
    assert replaced.previous_recipe_title == "Fiskesuppe"
    assert replaced.recipe_title == "Langtidsgryte"
    assert removed.status == "proposed"
    assert len(backend.list_meals(start, start + timedelta(days=6))) == 2

    # 2. Apply mode mutates backend immediately
    apply_box = WeekPlanToolbox(
        planning=backend,
        household=Household(default_servings=4),
        recipes=[soup, taco, stew],
        existing_meals=[existing_tue, existing_fri],
        start=start,
        days=7,
        apply_changes=True,
    )
    c1 = apply_box.add_meal(meal_date=start, recipe_id=taco.id, label="Lunsj")
    c2 = apply_box.replace_meal(meal_id=existing_tue.id, recipe_id=stew.id)
    c3 = apply_box.remove_meal(meal_id=existing_fri.id)
    assert all(c.status == "applied" for c in (c1, c2, c3))
    persisted = backend.list_meals(start, start + timedelta(days=6))
    assert len(persisted) == 2
    assert {m.recipe_id for m in persisted} == {taco.id, stew.id}


def test_toolbox_rejects_invalid_dates_recipes_and_meal_ids():
    start = date(2026, 11, 23)
    soup = _make_recipe("Tomatsuppe")
    backend = InMemoryWeekPlanningBackend(recipes=[soup], meals=[])
    toolbox = WeekPlanToolbox(
        planning=backend,
        household=Household(),
        recipes=[soup],
        existing_meals=[],
        start=start,
        days=7,
    )
    with pytest.raises(ValueError, match="outside the requested range"):
        toolbox.add_meal(meal_date=start + timedelta(days=7), recipe_id=soup.id)
    with pytest.raises(ValueError, match="not in the household catalog"):
        toolbox.add_meal(meal_date=start, recipe_id=uuid4())
    with pytest.raises(ValueError, match="not found in this week"):
        toolbox.remove_meal(meal_id=uuid4())
    with pytest.raises(ValueError, match="not found in this week"):
        toolbox.replace_meal(meal_id=uuid4(), recipe_id=soup.id)
    with pytest.raises(ValueError, match="servings must be positive"):
        toolbox.add_meal(meal_date=start, recipe_id=soup.id, servings=0)


@pytest.mark.asyncio
async def test_run_week_chat_multi_turn_executes_structured_tools_and_preserves_history():
    start = date(2026, 11, 23)
    salmon = _make_recipe("Laks i form", prep_time_minutes=20, prep_category="rask")
    pasta = _make_recipe("Kremet pasta", prep_time_minutes=15, prep_category="rask")
    existing = Meal(
        id=uuid4(),
        date=start + timedelta(days=2),
        recipe_id=pasta.id,
        servings=2,
        label="Middag",
    )
    backend = InMemoryWeekPlanningBackend(
        recipes=[salmon, pasta],
        meals=[existing],
        stats={
            salmon.id: RecipeActivityStats(
                recipe_id=salmon.id,
                average_rating=5.0,
                would_make_again=True,
                feedback_count=1,
            )
        },
    )
    toolbox = WeekPlanToolbox(
        planning=backend,
        household=Household(adults=2, children_ages=[6], dietary_preferences=["fisk to ganger"]),
        recipes=[salmon, pasta],
        existing_meals=[existing],
        start=start,
        days=7,
        apply_changes=True,
        recipe_stats=backend.get_recipes_stats(),
        inventory_items=[InventoryItem(id=uuid4(), name="Fløte", quantity=Decimal("3"), unit="dl")],
        evidence=[
            EvidenceSnippet(
                evidence_id="ev-1", text='{"kind":"feedback","message":"Barna elsket laks"}'
            )
        ],
        purchase_history=PurchaseHistoryContext(
            bought_before=[BoughtBeforeItem(provider="oda", product_name="Lakseporsjoner")]
        ),
    )

    captured_messages: list[list[ModelMessage]] = []
    model = _tool_turn_model(
        [
            ("list_available_recipes", {"max_prep_minutes": 25}),
            ("get_available_ingredients", {}),
            (
                "replace_meal",
                {
                    "meal_id": str(existing.id),
                    "recipe_id": str(salmon.id),
                    "servings": 3,
                },
            ),
            (
                "add_meal",
                {
                    "meal_date": start.isoformat(),
                    "recipe_id": str(pasta.id),
                    "servings": 3,
                    "label": "Middag",
                    "scheduled_time": "17:00",
                },
            ),
        ],
        reply="Jeg har lagt til Kremet pasta på mandag og byttet ut onsdag med Laks i form.",
        notes=["Brukte fløten dere har hjemme og tok hensyn til god tilbakemelding på laks."],
        captured=captured_messages,
    )
    agent = build_week_chat_agent(Settings(local_dev=True), model)
    response = await run_week_chat(
        agent,
        toolbox,
        "Bytt onsdag til laks og legg til rask pasta på mandag",
        history=[
            WeekChatTurn(role="user", content="Vi ønsker raske hverdagsmiddager."),
            WeekChatTurn(role="assistant", content="Den er god! Hvilke dager vil du planlegge?"),
        ],
    )

    assert "Laks i form" in response.reply
    assert response.applied is True
    assert [c.action for c in response.changes] == ["replace", "add"]
    assert response.changes[0].previous_recipe_title == "Kremet pasta"
    assert response.changes[0].recipe_title == "Laks i form"
    assert len(response.meals) == 2
    assert response.evidence_ids == ["ev-1"]
    assert captured_messages
    first_turn_messages = captured_messages[0]
    assert any(
        isinstance(part, UserPromptPart) and "raske hverdagsmiddager" in str(part.content)
        for msg in first_turn_messages
        for part in msg.parts
    )
    prompt_text = str(first_turn_messages[-1].parts[-1].content)
    assert "Fløte" in prompt_text
    assert "Barna elsket laks" in prompt_text
    assert "Lakseporsjoner" in prompt_text


@pytest.mark.asyncio
async def test_run_week_chat_handles_invalid_tool_call_without_mutating_plan():
    start = date(2026, 11, 23)
    soup = _make_recipe("Suppe")
    backend = InMemoryWeekPlanningBackend(recipes=[soup], meals=[])
    toolbox = WeekPlanToolbox(
        planning=backend,
        household=Household(),
        recipes=[soup],
        existing_meals=[],
        start=start,
        days=7,
        apply_changes=True,
    )
    model = _tool_turn_model(
        [("add_meal", {"meal_date": start.isoformat(), "recipe_id": str(uuid4())})],
        reply="Beklager, den oppskriften finnes ikke i samlingen.",
    )
    agent = build_week_chat_agent(Settings(local_dev=True), model)
    response = await run_week_chat(agent, toolbox, "Legg til ukjent oppskrift")
    assert response.changes == []
    assert response.applied is False
    assert backend.list_meals(start, start + timedelta(days=6)) == []


def test_apply_week_chat_changes_requires_reviewed_and_validates_atomically():
    start = date(2026, 11, 23)
    soup = _make_recipe("Suppe")
    taco = _make_recipe("Taco")
    existing = Meal(id=uuid4(), date=start, recipe_id=soup.id, servings=2)
    backend = InMemoryWeekPlanningBackend(recipes=[soup, taco], meals=[existing])

    with pytest.raises(ValueError, match="reviewed=true"):
        apply_week_chat_changes(
            planning=backend,
            request=WeekChatApplyRequest(
                start=start,
                days=7,
                reviewed=False,
                changes=[
                    WeekPlanChange(
                        action="add",
                        status="proposed",
                        date=start + timedelta(days=1),
                        recipe_id=taco.id,
                        recipe_title=taco.title,
                        servings=4,
                    )
                ],
            ),
        )

    # Second change references nonexistent meal -> first change must NOT apply
    with pytest.raises(ValueError, match="unavailable"):
        apply_week_chat_changes(
            planning=backend,
            request=WeekChatApplyRequest(
                start=start,
                days=7,
                reviewed=True,
                changes=[
                    WeekPlanChange(
                        action="add",
                        status="proposed",
                        date=start + timedelta(days=1),
                        recipe_id=taco.id,
                        recipe_title=taco.title,
                        servings=4,
                    ),
                    WeekPlanChange(
                        action="remove",
                        status="proposed",
                        meal_id=uuid4(),
                        date=start,
                        recipe_id=soup.id,
                        recipe_title=soup.title,
                        servings=2,
                    ),
                ],
            ),
        )
    assert len(backend.list_meals(start, start + timedelta(days=6))) == 1

    # Valid batch applies add + remove cleanly
    applied = apply_week_chat_changes(
        planning=backend,
        request=WeekChatApplyRequest(
            start=start,
            days=7,
            reviewed=True,
            changes=[
                WeekPlanChange(
                    action="add",
                    status="proposed",
                    date=start + timedelta(days=1),
                    recipe_id=taco.id,
                    recipe_title=taco.title,
                    servings=4,
                ),
                WeekPlanChange(
                    action="remove",
                    status="proposed",
                    meal_id=existing.id,
                    date=start,
                    recipe_id=soup.id,
                    recipe_title=soup.title,
                    servings=2,
                ),
            ],
        ),
    )
    assert len(applied.applied_changes) == 2
    assert all(c.status == "applied" for c in applied.applied_changes)
    assert [m.recipe_id for m in applied.meals] == [taco.id]


@pytest.mark.asyncio
async def test_ai_workflow_service_and_features_wire_chat_week_through_ports():
    start = date(2026, 11, 23)
    soup = _make_recipe("Fiskesuppe")
    backend = InMemoryWeekPlanningBackend(recipes=[soup], meals=[])
    fake_history = MagicMock()
    fake_history.engine = None
    fake_inventory = MagicMock()
    fake_inventory.list.return_value = [
        InventoryItem(id=uuid4(), name="Gulrot", quantity=Decimal("2"), unit="stk")
    ]
    fake_identities = MagicMock()
    fake_identities.list_correction_events.return_value = [
        IdentityCorrectionEvent(
            id=uuid4(),
            occurred_at=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
            household_id=uuid4(),
            actor_id="malo",
            kind="product",
            locale="nb",
            context="recipe:Fiskesuppe",
            normalized_alias="gulerot",
            alias="gulerot",
            new_identity_id=uuid4(),
        )
    ]

    wf = AiWorkflowService(
        kitchen=backend,
        settings=Settings(local_dev=True),
        household=Household,
        recipe_providers=MagicMock(),
        recipe_discovery=MagicMock(),
        history=fake_history,
        inventory=fake_inventory,
        catalogue_identities=fake_identities,
        purchase_sync=None,
    )
    wf.week_chat_agent = build_week_chat_agent(  # type: ignore[attr-defined]
        Settings(local_dev=True),
        _tool_turn_model(
            [("add_meal", {"meal_date": start.isoformat(), "recipe_id": str(soup.id)})],
            reply="La til Fiskesuppe på mandag.",
        ),
    )
    res = await wf.chat_week(
        WeekChatRequest(start=start, days=7, message="Legg til fiskesuppe på mandag")
    )
    assert res.applied is True
    assert len(res.changes) == 1
    assert len(res.identity_correction_citations) == 1

    # Also test KitchenFeatures.chat_week without DB engine
    fake_store = MagicMock()
    fake_store.engine = None
    fake_store.ensure_setting.return_value = str(uuid4())
    fake_kitchen = MagicMock(spec=KitchenService)
    fake_kitchen.store = fake_store
    fake_kitchen.household_id = uuid4()
    fake_kitchen.settings = Settings(local_dev=True)
    fake_kitchen.identities = MagicMock()
    fake_kitchen.identities.list_correction_events.return_value = []
    fake_kitchen.list_recipes.return_value = [soup]
    fake_kitchen.list_meals.return_value = []
    fake_kitchen.get_recipes_stats.return_value = {}

    features = KitchenFeatures(fake_kitchen, Settings(local_dev=True), Household)
    features.inventory = fake_inventory
    features.week_chat_agent = build_week_chat_agent(
        Settings(local_dev=True),
        TestModel(
            call_tools=[],
            custom_output_args={"reply": "Her er et forslag i Bokmål.", "notes": []},
        ),
    )
    features_res = await features.chat_week(
        WeekChatRequest(start=start, days=7, message="Hva kan vi spise i uken?")
    )
    assert features_res.reply == "Her er et forslag i Bokmål."
    assert features_res.applied is False


def test_api_week_chat_endpoints_handle_503_propose_apply_and_direct_changes():
    start = date(2026, 11, 23)
    soup = _make_recipe("Fiskesuppe")
    pasta = _make_recipe("Pasta")
    existing = Meal(id=uuid4(), date=start + timedelta(days=1), recipe_id=soup.id, servings=2)
    backend = InMemoryWeekPlanningBackend(recipes=[soup, pasta], meals=[existing])

    fake_store = MagicMock()
    fake_store.engine = None
    fake_store.ensure_setting.return_value = str(uuid4())
    fake_store.get_setting.return_value = None

    fake_kitchen = MagicMock(spec=KitchenService)
    fake_kitchen.store = fake_store
    fake_kitchen.household_id = uuid4()
    fake_kitchen.settings = Settings(local_dev=True)
    fake_kitchen.identities = MagicMock()
    fake_kitchen.identities.list_correction_events.return_value = []
    fake_kitchen.list_recipes.side_effect = backend.list_recipes
    fake_kitchen.list_meals.side_effect = backend.list_meals
    fake_kitchen.plan_meal.side_effect = backend.plan_meal
    fake_kitchen.update_meal.side_effect = backend.update_meal
    fake_kitchen.delete_meal.side_effect = backend.delete_meal
    fake_kitchen.get_recipes_stats.side_effect = backend.get_recipes_stats

    app = create_app(Settings(local_dev=True), service=fake_kitchen)
    unauthenticated_client = TestClient(app)
    assert (
        unauthenticated_client.post(
            "/api/ai/week-chat",
            json={"start": start.isoformat(), "days": 7, "message": "Planlegg uka"},
        ).status_code
        == 401
    )

    client = TestClient(app, client=("127.0.0.1", 50000))

    # 1. 503 when AI is not configured
    unconfigured = client.post(
        "/api/ai/week-chat",
        json={"start": start.isoformat(), "days": 7, "message": "Planlegg uka"},
    )
    assert unconfigured.status_code == 503

    # 2. Propose mode (apply_changes=False) does not mutate until /apply with reviewed=true
    app.state.features.week_chat_agent = build_week_chat_agent(
        Settings(local_dev=True),
        _tool_turn_model(
            [
                (
                    "replace_meal",
                    {"meal_id": str(existing.id), "recipe_id": str(pasta.id), "servings": 4},
                )
            ],
            reply="Jeg foreslår å bytte tirsdag til Pasta.",
        ),
    )
    proposed = client.post(
        "/api/ai/week-chat",
        json={
            "start": start.isoformat(),
            "days": 7,
            "message": "Bytt tirsdag til pasta",
            "apply_changes": False,
        },
    )
    assert proposed.status_code == 200
    proposed_body = proposed.json()
    assert proposed_body["applied"] is False
    assert proposed_body["changes"][0]["status"] == "proposed"
    assert backend.list_meals(start, start + timedelta(days=6))[0].recipe_id == soup.id

    # Missing reviewed=true -> 422
    rejected_apply = client.post(
        "/api/ai/week-chat/apply",
        json={
            "start": start.isoformat(),
            "days": 7,
            "changes": proposed_body["changes"],
            "reviewed": False,
        },
    )
    assert rejected_apply.status_code == 422

    # Explicit reviewed=true -> 200 and updates meal
    applied = client.post(
        "/api/ai/week-chat/apply",
        json={
            "start": start.isoformat(),
            "days": 7,
            "changes": proposed_body["changes"],
            "reviewed": True,
        },
    )
    assert applied.status_code == 200
    assert applied.json()["applied_changes"][0]["status"] == "applied"
    assert backend.list_meals(start, start + timedelta(days=6))[0].recipe_id == pasta.id

    # 3. Direct apply mode (apply_changes=True) executes add & remove in one turn
    current_meal_id = backend.list_meals(start, start + timedelta(days=6))[0].id
    app.state.features.week_chat_agent = build_week_chat_agent(
        Settings(local_dev=True),
        _tool_turn_model(
            [
                ("remove_meal", {"meal_id": str(current_meal_id)}),
                (
                    "add_meal",
                    {"meal_date": start.isoformat(), "recipe_id": str(soup.id), "servings": 3},
                ),
            ],
            reply="Fjernet tirsdag og la til Fiskesuppe på mandag.",
        ),
    )
    direct = client.post(
        "/api/ai/week-chat",
        json={
            "start": start.isoformat(),
            "days": 7,
            "message": "Fjern tirsdag og legg til fiskesuppe på mandag",
            "history": [
                {"role": "user", "content": "Bytt tirsdag til pasta"},
                {"role": "assistant", "content": "Jeg foreslår å bytte tirsdag til Pasta."},
            ],
            "apply_changes": True,
        },
    )
    assert direct.status_code == 200
    direct_body = direct.json()
    assert direct_body["applied"] is True
    assert [c["action"] for c in direct_body["changes"]] == ["remove", "add"]
    final_meals = backend.list_meals(start, start + timedelta(days=6))
    assert len(final_meals) == 1
    assert final_meals[0].recipe_id == soup.id
    assert final_meals[0].date == start
