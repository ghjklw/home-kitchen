from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from home_kitchen.ai import draft_recipe
from home_kitchen.models import PhotoSource, Recipe, RecipeCreate
from home_kitchen.recipes.models import EditableRecipeDraft
from home_kitchen.recipes.photo_storage import PhotoRecipeService, recipe_photos
from home_kitchen.recipes.photos import MAX_PHOTO_BYTES, PhotoExtraction
from home_kitchen.settings import Household, Settings
from home_kitchen.store import Store, recipes


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (8, 6), "red").save(output, format="PNG")
    return output.getvalue()


def draft():
    return EditableRecipeDraft(
        title="Soup",
        servings=2,
        ingredients=[{"name": "Salt", "source_indices": [0]}],
        instructions=["Mix"],
        source_ingredients=[{"index": 0, "text": "Salt"}],
    )


def service(database_url, *, agent=None):
    store = Store(database_url)
    return PhotoRecipeService(
        Settings(local_dev=True),
        uuid4(),
        store.engine,
        Household,
        agent=agent
        or SimpleNamespace(
            run=AsyncMock(return_value=SimpleNamespace(output=PhotoExtraction(draft=draft())))
        ),
    ), store


async def test_photo_private_scoped_persisted_and_explicit_save(test_database_url):
    photos, store = service(test_database_url)
    result = await photos.create(
        image_bytes(), "image/png", "actor", book_title="My book", page="12"
    )
    assert result.status == "draft_ready"
    assert store.list("recipe", Recipe) == []
    stored = await photos.get(result.photo_source.id)
    assert stored.data and stored.mime_type == "image/png"
    assert stored.book_title == "My book" and stored.page == "12"
    assert stored.created_by == "actor" and len(stored.sha256) == 64
    other, _ = service(test_database_url)
    with pytest.raises(KeyError):
        await other.get(stored.id)
    saved = Recipe(id=uuid4(), **result.draft.model_dump())
    store.save("recipe", saved)
    assert store.get("recipe", saved.id, Recipe).photo_source == result.photo_source
    with pytest.raises(IntegrityError):
        with store.connection() as connection:
            connection.execute(
                recipes.update().where(recipes.c.id == saved.id).values(photo_source_id=uuid4())
            )
    again = await photos.redraft(stored.id, "actor")
    assert again.photo_source == result.photo_source
    with store.connection() as connection:
        assert connection.scalar(select(func.count()).select_from(recipe_photos)) == 1


async def test_failure_retains_asset_and_hides_provider_details(test_database_url):
    agent = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("secret provider detail")))
    photos, store = service(test_database_url, agent=agent)
    result = await photos.create(image_bytes(), "image/png", "actor")
    assert result.status == "extraction_failed" and result.draft is None
    assert "secret" not in result.model_dump_json()
    assert (await photos.get(result.photo_source.id)).data
    assert store.list("recipe", Recipe) == []


async def test_concurrent_processing_rejects_without_queue(test_database_url):
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()

    async def run(*args, **kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(output=PhotoExtraction(draft=draft()))

    photos, _ = service(test_database_url, agent=SimpleNamespace(run=run))
    first = asyncio.create_task(photos.create(image_bytes(), "image/png", "actor"))
    await entered.wait()
    try:
        with pytest.raises(ValueError, match="busy"):
            await photos.create(image_bytes(), "image/png", "actor")
    finally:
        release.set()
    assert (await first).status == "draft_ready"


async def test_bounds_and_invalid_reference_do_not_save(test_database_url):
    photos, store = service(test_database_url)
    with pytest.raises(ValueError, match="8 MiB"):
        await photos.create(b"x" * (MAX_PHOTO_BYTES + 1), "image/png", "actor")
    with pytest.raises(ValueError, match="Actor"):
        await photos.create(image_bytes(), "image/png", "")
    with pytest.raises(ValidationError):
        RecipeCreate(**draft().model_dump(exclude={"photo_source"}), photo_source={"id": "invalid"})
    recipe = Recipe(
        id=uuid4(),
        **draft().model_dump(exclude={"photo_source"}),
        photo_source=PhotoSource(id=uuid4()),
    )
    with pytest.raises(ValueError, match="not found"):
        store.save("recipe", recipe)
    with store.connection() as connection:
        assert connection.scalar(select(func.count()).select_from(recipe_photos)) == 0


async def test_ai_adaptation_cannot_replace_private_reference():
    source = draft().model_copy(update={"photo_source": PhotoSource(id=uuid4(), page="7")})
    forged = draft().model_copy(update={"photo_source": PhotoSource(id=uuid4())})
    agent = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(output=forged)))
    adapted = await draft_recipe(agent, Household(), "Adapt it", source=source)
    assert adapted.photo_source == source.photo_source
