"""Authenticated REST and MCP transports for recipe acquisition."""

import asyncio
import base64
import json
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from fastmcp import Client
from PIL import Image
from sqlalchemy import create_engine, text

from home_kitchen.app import create_app
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.imports import ImportedDraft
from home_kitchen.recipes.collections import CollectionEntry, CollectionPage
from home_kitchen.recipes.images import RecipeImage, RecipeImageSearch, RecipeImageSearchSession
from home_kitchen.recipes.models import EditableRecipeDraft
from home_kitchen.recipes.photos import PhotoExtraction
from home_kitchen.settings import Settings


def png_bytes():
    stream = BytesIO()
    Image.new("RGB", (12, 8), "green").save(stream, format="PNG")
    return stream.getvalue()


def editable_draft():
    return EditableRecipeDraft(
        title="Soup",
        servings=2,
        ingredients=[{"name": "Tomato", "source_indices": [0]}],
        instructions=["Simmer"],
        source_ingredients=[{"index": 0, "text": "tomato"}],
    )


def image_candidate():
    return RecipeImage(
        provider_id="12345",
        query="tomato soup",
        title="File:Tomato soup.jpg",
        image_url="https://upload.wikimedia.org/wikipedia/commons/a/ab/Tomato.jpg",
        thumbnail_url="https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Tomato.jpg/640px-Tomato.jpg",
        source_page_url="https://commons.wikimedia.org/wiki/File:Tomato.jpg",
        artist="Ada Photographer",
        credit="Kitchen archive",
        license_name="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
        attribution_required=True,
        attribution="Ada Photographer",
        obligations=["Credit the creator and link the license."],
    )


class ImageProvider:
    async def search(self, query, *, limit=10, continuation=None):
        return RecipeImageSearch(status="ok", results=[image_candidate()])


class CollectionProvider:
    id = "oda"
    current = 1

    def generation(self, actor_id):
        return self.current

    async def page(self, actor_id, kind, page, size):
        entries = (
            [
                CollectionEntry(
                    id="731",
                    provider="oda",
                    title="Tomato soup",
                    source_url="https://oda.com/no/recipes/731-tomato-soup/",
                )
            ]
            if page == 1 and kind == "liked"
            else []
        )
        return CollectionPage(entries=entries, has_more=False)

    def lock_generation(self, connection, actor_id, generation):
        assert generation == self.current

    def validate_source(self, entry):
        from home_kitchen.recipes.collections import OdaCollectionProvider

        OdaCollectionProvider.validate_source(self, entry)


def app_for(database_url, *, local_dev=True, household_id=None):
    auth_settings = (
        {}
        if local_dev
        else {
            "entra_tenant_id": UUID("00000000-0000-0000-0000-000000000001"),
            "entra_audience": "api://home-kitchen",
            "entra_allowed_group": UUID("00000000-0000-0000-0000-000000000002"),
        }
    )
    if local_dev and household_id is not None:
        auth_settings["entra_allowed_group"] = household_id
    app = create_app(Settings(local_dev=local_dev, database_url=database_url, **auth_settings))
    app.state.features.recipe_images.provider = ImageProvider()
    app.state.features.recipe_collections.provider = CollectionProvider()

    async def extract(url):
        return ImportedDraft(
            title="Tomato soup",
            servings=2,
            ingredients=["2 tomatoes"],
            instructions=["Simmer"],
            source_url=url,
        )

    app.state.features.recipe_collections.extractor = extract
    app.state.features.recipe_photos.agent = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(output=PhotoExtraction(draft=editable_draft())))
    )
    return app


def settled_collection(client, job_id):
    for _ in range(100):
        response = client.get(f"/api/recipe-collections/{job_id}")
        if response.status_code != 200:
            pytest.fail(response.text)
        job = response.json()
        if job["status"] not in {"previewing", "importing"}:
            return job
        time.sleep(0.01)
    pytest.fail("collection operation did not finish")


def test_recipe_image_selection_persists_provider_attribution_without_overwriting_sources(
    test_database_url,
):
    app = app_for(test_database_url)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        upload = client.post(
            "/api/recipe-photos",
            json={"mime_type": "image/png", "data_base64": base64.b64encode(png_bytes()).decode()},
        )
        assert upload.status_code == 201
        photo_source = upload.json()["photo_source"]
        recipe_response = client.post(
            "/api/recipes",
            json={
                "title": "Adapted tomato soup",
                "servings": 2,
                "source": "ai",
                "source_url": "https://www.matprat.no/soup/",
                "ingredients": [{"name": "tomato", "quantity": "2", "unit": "item"}],
                "instructions": ["Simmer"],
                "source_ingredients": [{"index": 0, "text": "two tomatoes"}],
                "provenance": {
                    "original_source": "imported",
                    "source_url": "https://www.matprat.no/soup/",
                },
                "photo_source": photo_source,
            },
        )
        assert recipe_response.status_code == 201, recipe_response.text
        recipe = recipe_response.json()
        search_response = client.post("/api/recipe-images/search", json={"query": "tomato soup"})
        assert search_response.status_code == 200, search_response.text
        search = RecipeImageSearchSession.model_validate(search_response.json())
        candidate = search.search.results[0]

        rejected = client.post(
            f"/api/recipes/{recipe['id']}/image",
            json={"search_id": str(search.search_id), "provider_id": candidate.provider_id},
        )
        assert rejected.status_code == 422
        forged = client.post(
            f"/api/recipes/{recipe['id']}/image",
            json={
                "search_id": str(search.search_id),
                "provider_id": "caller-invented",
                "reviewed": True,
            },
        )
        assert forged.status_code == 422
        applied = client.post(
            f"/api/recipes/{recipe['id']}/image",
            json={
                "search_id": str(search.search_id),
                "provider_id": candidate.provider_id,
                "reviewed": True,
                "artist": "fabricated credit",
            },
        )
        assert applied.status_code == 200, applied.text
        saved = applied.json()
        assert saved["image_url"] == candidate.image_url
        assert saved["image_attribution"]["artist"] == "Ada Photographer"
        assert saved["image_attribution"]["license_name"] == "CC BY-SA 4.0"
        assert saved["photo_source"] == photo_source
        assert saved["source"] == "ai"
        assert saved["source_ingredients"] == recipe["source_ingredients"]
        assert saved["provenance"] == recipe["provenance"]

        # Generic recipe writes cannot manufacture or replace provider attribution.
        saved["image_attribution"]["artist"] = "fabricated credit"
        updated = client.put(f"/api/recipes/{recipe['id']}", json=saved)
        assert updated.status_code == 200
        assert updated.json()["image_attribution"]["artist"] == "Ada Photographer"
        saved["image_url"] = "https://images.example.org/other.jpg"
        changed = client.put(f"/api/recipes/{recipe['id']}", json=saved)
        assert changed.status_code == 200
        assert changed.json()["image_attribution"] is None


def test_image_attribution_migration_leaves_existing_recipe_rows_unattributed(test_database_url):
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = test_database_url
    command.downgrade(config, "0015_recipe_photos")
    recipe_id = UUID("d7a07e22-b4f8-4f84-9f86-c8d75c5b3fad")
    engine = create_engine(test_database_url)
    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO recipes (id,title,servings,ingredients,instructions,source,image_url)
                VALUES (:id,'Old soup',2,
                    '[{"name":"tomato","quantity":"2","unit":"item"}]'::jsonb,
                    '["Simmer"]'::jsonb,'manual',
                    'https://images.example.org/old.jpg')
            """),
            {"id": recipe_id},
        )
    command.upgrade(config, "head")
    from home_kitchen.service import KitchenService

    loaded = KitchenService(test_database_url).get_recipe(recipe_id)
    assert str(loaded.image_url) == "https://images.example.org/old.jpg"
    assert loaded.image_attribution is None
    engine.dispose()


def test_photo_and_collection_rest_routes_are_bounded_scoped_and_reviewed(test_database_url):
    app = app_for(test_database_url)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        integrations = client.get("/api/integrations")
        assert integrations.status_code == 200
        assert {item["id"] for item in integrations.json()} == {
            "oda",
            "anthropic",
            "cookidoo",
        }
        body = {"mime_type": "image/png", "data_base64": "%%%bad%%%"}
        assert client.post("/api/recipe-photos", json=body).status_code == 422
        too_large = {"mime_type": "image/png", "data_base64": "A" * 11_250_000}
        assert client.post("/api/recipe-photos", json=too_large).status_code == 413

        uploaded = client.post(
            "/api/recipe-photos",
            json={"mime_type": "image/png", "data_base64": base64.b64encode(png_bytes()).decode()},
        )
        assert uploaded.status_code == 201
        result = uploaded.json()
        assert "data" not in result
        photo_id = result["photo_source"]["id"]
        photo = client.get(f"/api/recipe-photos/{photo_id}")
        assert photo.status_code == 200
        assert photo.headers["content-type"] == "image/png"
        assert "no-store" in photo.headers["cache-control"]
        assert (
            photo.content == asyncio.run(app.state.features.recipe_photos.get(UUID(photo_id))).data
        )
        redrafted = client.post(f"/api/recipe-photos/{photo_id}/redraft")
        assert redrafted.status_code == 200

        unauthorized_app = app_for(test_database_url, local_dev=False)
        with TestClient(unauthorized_app, client=("203.0.113.10", 5000)) as unauthenticated:
            assert (
                unauthenticated.post(
                    "/api/recipe-images/search", json={"query": "soup"}
                ).status_code
                == 401
            )
            assert unauthenticated.get(f"/api/recipe-photos/{photo_id}").status_code == 401

        preview = client.post("/api/recipe-collections", json={"kinds": ["liked"]})
        assert preview.status_code == 202, preview.text
        preview_job = settled_collection(client, preview.json()["job_id"])
        assert preview_job["entries"][0]["id"] == "731"
        denied = client.post(
            f"/api/recipe-collections/{preview_job['job_id']}/import",
            json={"selected_ids": ["731"], "reviewed": False},
        )
        assert denied.status_code == 422
        imported = client.post(
            f"/api/recipe-collections/{preview_job['job_id']}/import",
            json={"selected_ids": ["731"], "reviewed": True},
        )
        assert imported.status_code == 202, imported.text
        completed = settled_collection(client, preview_job["job_id"])
        assert completed["entries"][0]["status"] == "imported"
        cancelled = client.post(f"/api/recipe-collections/{preview_job['job_id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["entries"][0]["status"] == "imported"


@pytest.mark.asyncio
async def test_mcp_acquisition_tools_are_registered_actor_scoped_and_never_return_photo_bytes(
    test_database_url,
):
    app = app_for(test_database_url)
    async with Client(app.state.mcp) as mcp:
        tools = {tool.name for tool in await mcp.list_tools()}
        assert {
            "upload_recipe_photo",
            "redraft_recipe_photo",
            "discard_recipe_photo",
            "preview_recipe_collections",
            "get_recipe_collection_preview",
            "import_recipe_collection_selection",
            "cancel_recipe_collection",
            "search_recipe_images",
            "select_recipe_image",
        } <= tools

        token = history_context.set(HistoryContext(actor_id="alice", channel="mcp"))
        try:
            searched = await app.state.mcp._tool_manager.call_tool(
                "search_recipe_images", {"query": "tomato soup"}
            )
            assert searched.structured_content["search"]["results"]
            upload = await app.state.mcp._tool_manager.call_tool(
                "upload_recipe_photo",
                {
                    "data_base64": base64.b64encode(png_bytes()).decode(),
                    "mime_type": "image/png",
                },
            )
            result_json = json.dumps(upload.structured_content)
            assert "data_base64" not in result_json and '"data"' not in result_json
            upload_result = upload.structured_content
            photo_id = upload_result["photo_source"]["id"]
            await app.state.mcp._tool_manager.call_tool(
                "redraft_recipe_photo", {"photo_id": photo_id}
            )
            discarded = await app.state.mcp._tool_manager.call_tool(
                "discard_recipe_photo", {"photo_id": photo_id}
            )
            assert discarded.structured_content["deleted"] is True

            preview = await app.state.mcp._tool_manager.call_tool(
                "preview_recipe_collections", {"kinds": ["liked"]}
            )
            preview_result = preview.structured_content
            job_id = preview_result["job_id"]
            await asyncio.sleep(0.05)
            ready = await app.state.mcp._tool_manager.call_tool(
                "get_recipe_collection_preview", {"job_id": job_id}
            )
            ready_result = ready.structured_content
            assert ready_result["status"] == "ready"
            from fastmcp.exceptions import ToolError

            with pytest.raises(ToolError):
                await app.state.mcp._tool_manager.call_tool(
                    "import_recipe_collection_selection",
                    {"job_id": job_id, "selected_ids": ["731"], "reviewed": False},
                )
            allowed_import = await app.state.mcp._tool_manager.call_tool(
                "import_recipe_collection_selection",
                {"job_id": job_id, "selected_ids": ["731"], "reviewed": True},
            )
            assert allowed_import.structured_content["status"] in {"importing", "complete"}
            await asyncio.sleep(0.05)
        finally:
            history_context.reset(token)

        token = history_context.set(HistoryContext(actor_id="bob", channel="mcp"))
        try:
            with pytest.raises(ToolError):
                await app.state.mcp._tool_manager.call_tool(
                    "get_recipe_collection_preview", {"job_id": job_id}
                )
        finally:
            history_context.reset(token)

        token = history_context.set(HistoryContext(actor_id="alice", channel="mcp"))
        try:
            own = await app.state.mcp._tool_manager.call_tool(
                "get_recipe_collection_preview", {"job_id": job_id}
            )
            assert own.structured_content["status"] in {"ready", "completed"}
            cancelled = await app.state.mcp._tool_manager.call_tool(
                "cancel_recipe_collection", {"job_id": job_id}
            )
            assert cancelled.structured_content["status"] == "cancelled"
        finally:
            history_context.reset(token)


def test_photo_discard_rest_contract_preserves_saved_sources(test_database_url):
    app = app_for(test_database_url)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        body = {
            "mime_type": "image/png",
            "data_base64": base64.b64encode(png_bytes()).decode(),
        }
        abandoned = client.post("/api/recipe-photos", json=body)
        assert abandoned.status_code == 201
        abandoned_id = abandoned.json()["photo_source"]["id"]
        other_household = app_for(test_database_url, household_id=UUID(int=1))
        with TestClient(other_household, client=("127.0.0.1", 5000)) as other_client:
            cross_household = other_client.delete(f"/api/recipe-photos/{abandoned_id}")
            assert cross_household.status_code == 404
            assert cross_household.json() == {"detail": "Item not found"}

        deleted = client.delete(f"/api/recipe-photos/{abandoned_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}
        missing = client.delete(f"/api/recipe-photos/{abandoned_id}")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Item not found"}

        saved = client.post("/api/recipe-photos", json=body)
        assert saved.status_code == 201
        source = saved.json()["photo_source"]
        recipe = client.post(
            "/api/recipes",
            json={
                "title": "Soup",
                "servings": 2,
                "ingredients": [
                    {"name": "Tomato", "quantity": "1", "unit": "item", "source_indices": [0]}
                ],
                "instructions": ["Simmer"],
                "source_ingredients": [{"index": 0, "text": "tomato"}],
                "photo_source": source,
            },
        )
        assert recipe.status_code == 201, recipe.text
        protected = client.delete(f"/api/recipe-photos/{source['id']}")
        assert protected.status_code == 200
        assert protected.json() == {"deleted": False}
