"""Source attribution remains editable data through explicit save and reload."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from sqlalchemy import text

from home_kitchen.ai import recipe_agent
from home_kitchen.app import create_app
from home_kitchen.history import HistoryRepository
from home_kitchen.models import RecipeCreate
from home_kitchen.recipes import RecipeContext, RecipeDraftRequest, create_recipe_providers
from home_kitchen.recipes.models import EditableRecipeDraft
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings


def source_recipe():
    return {
        "title": "Adapted soup", "servings": 2, "source": "ai",
        "source_url": "https://www.matprat.no/soup/",
        "ingredients": [{
            "name": "salt", "quantity": "2", "unit": "g", "source_indices": [0],
        }],
        "instructions": ["Simmer"],
        "source_ingredients": [{"index": 0, "text": "a pinch of salt"}],
        "provenance": {
            "kind": "ai_adaptation", "original_source": "imported",
            "source_url": "https://www.matprat.no/soup/",
        },
    }


def test_explicit_save_edit_restart_retains_original_attribution_and_history(test_database_url):
    settings = Settings(local_dev=True, database_url=test_database_url)
    with TestClient(create_app(settings), client=("127.0.0.1", 5000)) as client:
        response = client.post("/api/recipes", json=source_recipe())
        assert response.status_code == 201
        saved = response.json()
        saved["title"] = "Reviewed soup"
        saved["ingredients"][0]["quantity"] = "3"
        updated = client.put(f"/api/recipes/{saved['id']}", json=saved)
        assert updated.status_code == 200
        expected = updated.json()
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        loaded = client.get(f"/api/recipes/{saved['id']}").json()
        assert loaded == expected
        assert loaded["source_ingredients"] == source_recipe()["source_ingredients"]
        assert loaded["provenance"] == source_recipe()["provenance"]
        assert loaded["ingredients"][0]["source_indices"] == [0]
        # A nullable draft remains distinct from a saveable recipe.
        unresolved = {**loaded, "ingredients": [{"name": "salt", "quantity": None, "unit": None}]}
        assert client.put(f"/api/recipes/{saved['id']}", json=unresolved).status_code == 422
        assert client.get(f"/api/recipes/{saved['id']}").json() == expected
        history = HistoryRepository(app.state.service.store.engine).list()
        latest_save = next(entry for entry in history if entry.structured.get("entity_type") == "recipe")
        assert latest_save.structured["data"]["provenance"] == source_recipe()["provenance"]


async def test_saved_ai_source_and_repeat_iteration_keep_original_records(test_database_url):
    service = KitchenService(test_database_url)
    saved = service.create_recipe(RecipeCreate.model_validate(source_recipe()))
    service.store.engine.dispose()
    restarted = KitchenService(test_database_url)
    loaded = restarted.get_recipe(saved.id)
    settings = Settings(local_dev=True)
    model = TestModel(custom_output_args={
        **source_recipe(), "title": "Proposed next soup", "servings": 200,
        "ingredients": [{"name": "salt", "quantity": None, "unit": None, "source_indices": [0]}],
    })
    providers = create_recipe_providers(settings, recipe_agent(settings, model))
    context = RecipeContext(household=Household())
    first = await providers.draft("ai", RecipeDraftRequest(recipe=loaded, prompt="Simplify"), context)
    assert first.adapted.source_ingredients == loaded.source_ingredients
    assert first.adapted.provenance.original_source == "imported"
    assert first.adapted.ingredients[0].source_indices == [0]
    second = await providers.draft("ai", RecipeDraftRequest(draft=first.adapted), context)
    assert second.adapted.source_ingredients == loaded.source_ingredients
    assert second.adapted.provenance == loaded.provenance
    assert second.adapted.servings == loaded.servings
    assert restarted.get_recipe(saved.id) == loaded
    restarted.store.engine.dispose()


@pytest.mark.parametrize("mutation, message", [
    ({"source_ingredients": [{"index": 0, "text": "salt"}, {"index": 0, "text": "salt"}]}, "unique"),
    ({"source_ingredients": []}, "must reference"),
    ({"provenance": {"original_source": "imported", "source_url": "https://wrong.example/"}}, "must match"),
    ({"ingredients": [{"name": "salt", "quantity": "2", "source_indices": [-1]}]}, "greater than or equal"),
])
def test_domain_and_draft_validate_source_metadata(mutation, message):
    payload = {**source_recipe(), **mutation}
    for schema in (RecipeCreate, EditableRecipeDraft):
        with pytest.raises(ValidationError, match=message):
            schema.model_validate(payload)


def test_provenance_migration_preserves_old_rows_without_rewriting_ingredients(test_database_url):
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = test_database_url
    command.downgrade(config, "0008_mcp_oauth")
    service = KitchenService(test_database_url)
    old_id = uuid4()
    original_ingredients = [{"name": "carrot", "quantity": "2", "unit": "item"}]
    with service.store.engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO recipes (id,title,servings,ingredients,instructions,source)
            VALUES (:id,'Old soup',2,CAST(:ingredients AS jsonb),'["Simmer"]'::jsonb,'manual')
        """), {"id": old_id, "ingredients": json.dumps(original_ingredients)})
    command.upgrade(config, "head")
    loaded = service.get_recipe(old_id)
    assert loaded.source_ingredients == []
    assert loaded.provenance is None
    assert loaded.ingredients[0].source_indices == []
    with service.store.engine.connect() as connection:
        stored = connection.execute(text("SELECT ingredients FROM recipes WHERE id=:id"), {"id": old_id}).scalar_one()
    assert stored == original_ingredients
    service.store.engine.dispose()
