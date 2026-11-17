"""Unit and migration tests for recipe metadata (recipe_type, prep_time_minutes, prep_category)."""

import runpy
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from starlette.testclient import TestClient

from home_kitchen.ai import draft_recipe, recipe_agent
from home_kitchen.app import create_app
from home_kitchen.mcp_server import build_mcp
from home_kitchen.models import (
    PrepCategory,
    Recipe,
    RecipeCreate,
    RecipeType,
)
from home_kitchen.recipes.models import EditableRecipeDraft
from home_kitchen.recommendations import build_planner, recommend_plan
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings


def sample_recipe_data(**overrides) -> dict:
    base = {
        "title": "Pasta Pomodoro",
        "servings": 2,
        "ingredients": [
            {"name": "pasta", "quantity": 200, "unit": "g"},
            {"name": "tomato", "quantity": 400, "unit": "g"},
        ],
        "instructions": ["Boil water and cook pasta", "Simmer tomatoes and serve"],
    }
    base.update(overrides)
    return base


def make_recipe(
    recipe_type: RecipeType | str | None = None,
    prep_time_minutes: int | None = None,
    prep_category: PrepCategory | str | None = None,
    **kwargs,
) -> Recipe:
    data = sample_recipe_data(
        recipe_type=recipe_type,
        prep_time_minutes=prep_time_minutes,
        prep_category=prep_category,
        **kwargs,
    )
    return Recipe(id=uuid4(), **RecipeCreate.model_validate(data).model_dump())


# ==============================================================================
# 1. Data Model & Validation Tests
# ==============================================================================


def test_recipe_model_supports_optional_metadata():
    create = RecipeCreate.model_validate(sample_recipe_data())
    assert create.recipe_type is None
    assert create.prep_time_minutes is None
    assert create.prep_category is None

    recipe = Recipe(id=uuid4(), **create.model_dump())
    assert recipe.recipe_type is None
    assert recipe.prep_time_minutes is None
    assert recipe.prep_category is None


def test_recipe_model_accepts_valid_recipe_types():
    expected_types = [
        ("middag", RecipeType.MIDDAG),
        ("dessert", RecipeType.DESSERT),
        ("frokost", RecipeType.FROKOST),
        ("lunsj", RecipeType.LUNSJ),
        ("tilbehør", RecipeType.TILBEHOR),
        ("tilbehor", RecipeType.TILBEHOR),
        ("bakst", RecipeType.BAKST),
        ("annet", RecipeType.ANNET),
        ("  Middag  ", RecipeType.MIDDAG),
    ]
    for raw_type, expected_enum in expected_types:
        rc = RecipeCreate.model_validate(sample_recipe_data(recipe_type=raw_type))
        assert rc.recipe_type == expected_enum


def test_recipe_model_prep_time_minutes_bounds():
    # Valid bounds: 1 to 2880
    rc_min = RecipeCreate.model_validate(sample_recipe_data(prep_time_minutes=1))
    assert rc_min.prep_time_minutes == 1

    rc_max = RecipeCreate.model_validate(sample_recipe_data(prep_time_minutes=2880))
    assert rc_max.prep_time_minutes == 2880

    with pytest.raises(ValidationError):
        RecipeCreate.model_validate(sample_recipe_data(prep_time_minutes=0))

    with pytest.raises(ValidationError):
        RecipeCreate.model_validate(sample_recipe_data(prep_time_minutes=-10))

    with pytest.raises(ValidationError):
        RecipeCreate.model_validate(sample_recipe_data(prep_time_minutes=2881))


def test_recipe_model_auto_derives_prep_category_when_omitted():
    # <= 30 -> "rask"
    rc_rask_1 = RecipeCreate.model_validate(sample_recipe_data(prep_time_minutes=15))
    assert rc_rask_1.prep_category == PrepCategory.RASK

    rc_rask_30 = RecipeCreate.model_validate(sample_recipe_data(prep_time_minutes=30))
    assert rc_rask_30.prep_category == PrepCategory.RASK

    # <= 75 -> "normal"
    rc_normal_31 = RecipeCreate.model_validate(sample_recipe_data(prep_time_minutes=31))
    assert rc_normal_31.prep_category == PrepCategory.NORMAL

    rc_normal_75 = RecipeCreate.model_validate(sample_recipe_data(prep_time_minutes=75))
    assert rc_normal_75.prep_category == PrepCategory.NORMAL

    # > 75 -> "langtid"
    rc_langtid_76 = RecipeCreate.model_validate(sample_recipe_data(prep_time_minutes=76))
    assert rc_langtid_76.prep_category == PrepCategory.LANGTID

    rc_langtid_120 = RecipeCreate.model_validate(sample_recipe_data(prep_time_minutes=120))
    assert rc_langtid_120.prep_category == PrepCategory.LANGTID


def test_recipe_model_preserves_explicit_prep_category():
    # When prep_category is provided, auto-derivation does not override it
    rc = RecipeCreate.model_validate(
        sample_recipe_data(prep_time_minutes=45, prep_category="batch_cooking")
    )
    assert rc.prep_category == PrepCategory.BATCH_COOKING
    assert rc.prep_time_minutes == 45

    rc_rask = RecipeCreate.model_validate(
        sample_recipe_data(prep_time_minutes=90, prep_category="rask")
    )
    assert rc_rask.prep_category == PrepCategory.RASK


def test_recipe_model_prep_category_normalization():
    rc1 = RecipeCreate.model_validate(sample_recipe_data(prep_category="  Batch Cooking  "))
    assert rc1.prep_category == PrepCategory.BATCH_COOKING

    rc2 = RecipeCreate.model_validate(sample_recipe_data(prep_category="batch-cooking"))
    assert rc2.prep_category == PrepCategory.BATCH_COOKING

    rc3 = RecipeCreate.model_validate(sample_recipe_data(prep_category="  RASK  "))
    assert rc3.prep_category == PrepCategory.RASK


def test_editable_recipe_draft_normalizes_and_derives_metadata():
    draft = EditableRecipeDraft.model_validate(
        {
            "title": "Hjemmelaget aioli",
            "servings": 4,
            "ingredients": [{"name": "eggeplomme"}],
            "instructions": ["Visp sammen"],
            "recipe_type": "  Tilbehor ",
            "prep_time_minutes": 15,
        }
    )
    assert draft.recipe_type == RecipeType.TILBEHOR
    assert draft.prep_category == PrepCategory.RASK

    batch_draft = EditableRecipeDraft.model_validate(
        {
            "title": "Lasagne",
            "servings": 8,
            "ingredients": [{"name": "pastaplater"}],
            "instructions": ["Stek i ovn"],
            "prep_time_minutes": 90,
            "prep_category": "batch-cooking",
        }
    )
    assert batch_draft.prep_category == PrepCategory.BATCH_COOKING


def test_store_entity_values_maps_recipe_metadata():
    from home_kitchen.store import entity_values

    with_meta = make_recipe(
        recipe_type="tilbehor",
        prep_time_minutes=45,
        prep_category="batch-cooking",
    )
    values = entity_values(with_meta)
    assert values["recipe_type"] == "tilbehør"
    assert values["prep_time_minutes"] == 45
    assert values["prep_category"] == "batch_cooking"

    without_meta = make_recipe()
    empty_values = entity_values(without_meta)
    assert empty_values["recipe_type"] is None
    assert empty_values["prep_time_minutes"] is None
    assert empty_values["prep_category"] is None


# ==============================================================================
# 2. Service CRUD & Filtering Tests
# ==============================================================================


def test_service_preserves_metadata_on_create_and_update():
    mock_store = MagicMock()
    mock_store.list.return_value = []
    service = KitchenService(store=mock_store)

    # 1. Create recipe with metadata
    data = RecipeCreate.model_validate(
        sample_recipe_data(
            title="Linsesuppe",
            recipe_type="middag",
            prep_time_minutes=25,
        )
    )
    created = service.create_recipe(data)
    assert created.recipe_type == RecipeType.MIDDAG
    assert created.prep_time_minutes == 25
    assert created.prep_category == PrepCategory.RASK

    saved_entity = mock_store.save.call_args[0][1]
    assert saved_entity.recipe_type == RecipeType.MIDDAG
    assert saved_entity.prep_time_minutes == 25
    assert saved_entity.prep_category == PrepCategory.RASK

    # 2. Update recipe with updated metadata
    mock_store.get.return_value = created
    update_data = RecipeCreate.model_validate(
        sample_recipe_data(
            title="Langtidsstekt gryte",
            recipe_type="middag",
            prep_time_minutes=180,
            prep_category="batch_cooking",
        )
    )
    updated = service.update_recipe(created.id, update_data)
    assert updated.recipe_type == RecipeType.MIDDAG
    assert updated.prep_time_minutes == 180
    assert updated.prep_category == PrepCategory.BATCH_COOKING


def test_service_list_recipes_filtering():
    r_quick_dinner = make_recipe(
        title="Quick Tacos",
        recipe_type="middag",
        prep_time_minutes=20,
    )
    r_slow_dinner = make_recipe(
        title="Beef Stew",
        recipe_type="middag",
        prep_time_minutes=120,
    )
    r_batch_cooking = make_recipe(
        title="Chili con Carne",
        recipe_type="middag",
        prep_time_minutes=90,
        prep_category="batch_cooking",
    )
    r_dessert = make_recipe(
        title="Chocolate Mousse",
        recipe_type="dessert",
        prep_time_minutes=25,
    )
    r_baking = make_recipe(
        title="Sourdough Bread",
        recipe_type="bakst",
        prep_time_minutes=360,
    )
    r_legacy = make_recipe(
        title="Old Family Recipe",
        recipe_type=None,
        prep_time_minutes=None,
    )

    all_recipes = [
        r_quick_dinner,
        r_slow_dinner,
        r_batch_cooking,
        r_dessert,
        r_baking,
        r_legacy,
    ]

    mock_store = MagicMock()
    mock_store.list.return_value = all_recipes
    service = KitchenService(store=mock_store)

    # 1. No filters -> returns all
    assert len(service.list_recipes()) == 6

    # 2. Filter by recipe_type
    middag = service.list_recipes(recipe_type="middag")
    assert len(middag) == 3
    assert {r.title for r in middag} == {"Quick Tacos", "Beef Stew", "Chili con Carne"}

    dessert = service.list_recipes(recipe_type="dessert")
    assert len(dessert) == 1
    assert dessert[0].title == "Chocolate Mousse"

    # 3. Filter by prep_category
    rask = service.list_recipes(prep_category="rask")
    assert len(rask) == 2
    assert {r.title for r in rask} == {"Quick Tacos", "Chocolate Mousse"}

    batch = service.list_recipes(prep_category="batch_cooking")
    assert len(batch) == 1
    assert batch[0].title == "Chili con Carne"

    langtid = service.list_recipes(prep_category="langtid")
    assert len(langtid) == 2
    assert {r.title for r in langtid} == {"Beef Stew", "Sourdough Bread"}

    # 4. Filter by max_prep_minutes
    under_30 = service.list_recipes(max_prep_minutes=30)
    assert len(under_30) == 2
    assert {r.title for r in under_30} == {"Quick Tacos", "Chocolate Mousse"}

    under_100 = service.list_recipes(max_prep_minutes=100)
    assert len(under_100) == 3
    assert {r.title for r in under_100} == {"Quick Tacos", "Chili con Carne", "Chocolate Mousse"}

    # 5. Combined filters: quick weekday dinner
    quick_dinners = service.list_recipes(
        recipe_type="middag",
        prep_category="rask",
        max_prep_minutes=30,
    )
    assert len(quick_dinners) == 1
    assert quick_dinners[0].title == "Quick Tacos"

    # 6. Alias & blank normalization in filters
    assert {r.title for r in service.list_recipes(prep_category="batch-cooking")} == {
        "Chili con Carne"
    }
    side = make_recipe(title="Garlic Dip", recipe_type="tilbehør", prep_time_minutes=10)
    mock_store.list.return_value = [*all_recipes, side]
    assert {r.title for r in service.list_recipes(recipe_type="tilbehor")} == {"Garlic Dip"}
    assert len(service.list_recipes(recipe_type="   ", prep_category="")) == 7


# ==============================================================================
# 3. FastMCP and API Filtering Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_mcp_list_recipes_with_filters():
    r1 = make_recipe(title="Quick Tacos", recipe_type="middag", prep_time_minutes=20)

    mock_service = MagicMock()
    mock_service.list_recipes.return_value = [r1]

    mcp = build_mcp(mock_service, get_household=lambda: Household())
    async with Client(mcp) as client:
        res = await client.call_tool(
            "list_recipes",
            {"recipe_type": "middag", "prep_category": "rask", "max_prep_minutes": 30},
        )
        content = res.structured_content
        data = content.get("result", content)
        assert len(data) == 1
        assert data[0]["title"] == "Quick Tacos"
        mock_service.list_recipes.assert_called_once_with(
            recipe_type="middag", prep_category="rask", max_prep_minutes=30
        )


@pytest.mark.asyncio
async def test_mcp_list_recipes_rejects_out_of_bounds_max_prep_minutes():
    mock_service = MagicMock()
    mcp = build_mcp(mock_service, get_household=lambda: Household())
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("list_recipes", {"max_prep_minutes": 0})
        with pytest.raises(ToolError):
            await client.call_tool("list_recipes", {"max_prep_minutes": 2881})
    mock_service.list_recipes.assert_not_called()


def test_app_api_recipes_filtering_query_params():
    class MemoryTestStore:
        def __init__(self):
            self.settings = {}
            self.photo_household_id = None
            self.engine = None

        def get_setting(self, key: str) -> str | None:
            return self.settings.get(key)

        def set_setting(self, key: str, value: str) -> None:
            self.settings[key] = value

        def ensure_setting(self, key: str, value: str) -> str:
            if key not in self.settings:
                self.settings[key] = value
            return self.settings[key]

        def list(self, kind: str, model: type) -> list:
            return []

    r1 = make_recipe(title="Quick Tacos", recipe_type="middag", prep_time_minutes=20)
    mock_store = MemoryTestStore()
    kitchen = KitchenService(store=mock_store)
    kitchen.list_recipes = MagicMock(return_value=[r1])

    app = create_app(Settings(local_dev=True, entra_allowed_group=uuid4()), service=kitchen)
    client = TestClient(app, client=("127.0.0.1", 5000))

    response = client.get(
        "/api/recipes",
        params={"recipe_type": "middag", "prep_category": "rask", "max_prep_minutes": 30},
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["title"] == "Quick Tacos"
    kitchen.list_recipes.assert_called_once_with(
        recipe_type="middag", prep_category="rask", max_prep_minutes=30
    )

    assert client.get("/api/recipes", params={"max_prep_minutes": 0}).status_code == 422
    assert client.get("/api/recipes", params={"max_prep_minutes": 2881}).status_code == 422


# ==============================================================================
# 4. AI Meal Planning Context & Prompt Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_recommend_plan_includes_metadata_in_recipe_catalog():
    r_weekday = make_recipe(
        title="Laksewraps",
        recipe_type="middag",
        prep_time_minutes=20,
    )
    r_weekend = make_recipe(
        title="Biff Bourguignon",
        recipe_type="middag",
        prep_time_minutes=150,
        prep_category="batch_cooking",
    )

    meal = {
        "date": "2026-03-02",
        "slot": "dinner",
        "recipe_id": str(r_weekday.id),
        "servings": 4,
    }

    captured_prompt = None

    class CaptureAgent:
        async def run(self, prompt, **kwargs):
            nonlocal captured_prompt
            captured_prompt = prompt
            from home_kitchen.recommendations import PlanDraft

            mock_res = MagicMock()
            mock_res.output = PlanDraft(meals=[meal])
            return mock_res

    start = date(2026, 3, 2)
    draft = await recommend_plan(
        CaptureAgent(),
        Household(),
        [r_weekday, r_weekend],
        start,
        days=7,
    )
    assert len(draft.meals) == 1
    assert captured_prompt is not None

    # Check that recipe_type, prep_time_minutes, prep_category appear in the prompt context
    assert "'recipe_type': 'middag'" in captured_prompt
    assert "'prep_time_minutes': 20" in captured_prompt
    assert "'prep_category': 'rask'" in captured_prompt
    assert "'prep_time_minutes': 150" in captured_prompt
    assert "'prep_category': 'batch_cooking'" in captured_prompt


def test_build_planner_instructions_mention_weekday_quick_and_weekend_long():
    agent = build_planner(Settings(local_dev=True), TestModel())
    instructions = (
        " ".join(item.instruction for item in agent._instructions)
        if isinstance(agent._instructions, list)
        else str(agent._instructions)
    )
    assert "weekdays" in instructions or "Monday" in instructions
    assert "weekends" in instructions or "Saturday" in instructions
    assert "rask" in instructions
    assert "batch_cooking" in instructions or "langtid" in instructions


@pytest.mark.asyncio
async def test_recipe_agent_and_draft_preserve_recipe_metadata():
    source = EditableRecipeDraft(
        title="Source soup",
        servings=2,
        ingredients=[{"name": "salt"}],
        instructions=["Simmer"],
        recipe_type=RecipeType.MIDDAG,
        prep_time_minutes=25,
        prep_category=PrepCategory.RASK,
    )

    draft = await draft_recipe(
        recipe_agent(
            Settings(local_dev=True),
            TestModel(
                custom_output_args={
                    "title": "Soup",
                    "servings": 99,
                    "ingredients": [{"name": "salt", "quantity": None, "unit": None}],
                    "instructions": ["Simmer"],
                }
            ),
        ),
        Household(),
        "Keep metadata intact",
        source=source,
    )
    assert draft.recipe_type == RecipeType.MIDDAG
    assert draft.prep_time_minutes == 25
    assert draft.prep_category == PrepCategory.RASK


# ==============================================================================
# 5. Alembic Migration Structure Tests
# ==============================================================================


def test_alembic_migration_0026_metadata():
    migration_path = (
        Path(__file__).parents[1] / "migrations" / "versions" / "0026_recipe_metadata.py"
    )
    assert migration_path.exists()

    migration = runpy.run_path(str(migration_path))
    assert migration["revision"] == "0026_recipe_metadata"
    assert len(migration["revision"]) <= 32
    assert migration["down_revision"] == "0025_connector_credentials"

    # Test upgrade and downgrade with mock op
    with (
        patch.object(migration["op"], "add_column") as mock_add,
        patch.object(migration["op"], "drop_column") as mock_drop,
    ):
        migration["upgrade"]()
        assert mock_add.call_count == 3
        added_cols = [call[0][1].name for call in mock_add.call_args_list]
        assert "recipe_type" in added_cols
        assert "prep_time_minutes" in added_cols
        assert "prep_category" in added_cols

        migration["downgrade"]()
        assert mock_drop.call_count == 3
        dropped_cols = [call[0][1] for call in mock_drop.call_args_list]
        assert "prep_category" in dropped_cols
        assert "prep_time_minutes" in dropped_cols
        assert "recipe_type" in dropped_cols


def test_alembic_single_head_includes_0026_recipe_metadata():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    revisions = {rev.revision for rev in script.walk_revisions()}
    assert "0026_recipe_metadata" in revisions
