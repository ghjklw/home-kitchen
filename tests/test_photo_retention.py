"""Private draft retention, including PostgreSQL transaction races."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_photo_storage import draft, image_bytes, service

from home_kitchen.history import HistoryRepository
from home_kitchen.models import PhotoSource, Recipe
from home_kitchen.recipes.photo_storage import (
    PHOTO_CLEANUP_BATCH_SIZE,
    PHOTO_DRAFT_TTL,
    recipe_photos,
)
from home_kitchen.store import recipes

pytestmark = pytest.mark.mocked_provider


def age(store, photo_id, at):
    with store.connection() as connection:
        connection.execute(
            recipe_photos.update()
            .where(recipe_photos.c.id == photo_id)
            .values(last_draft_activity_at=at)
        )


async def upload(photos):
    return await photos.create(image_bytes(), "image/png", "actor")


async def test_expiry_boundary_and_household_isolation(test_database_url):
    photos, store = service(test_database_url)
    other, _ = service(test_database_url)
    result = await upload(photos)
    at = datetime.now(UTC)
    age(store, result.photo_source.id, at)
    assert other.cleanup(at + PHOTO_DRAFT_TTL) == 0
    assert photos.cleanup(at + PHOTO_DRAFT_TTL - timedelta(microseconds=1)) == 0
    assert photos.cleanup(at + PHOTO_DRAFT_TTL) == 1
    assert photos.cleanup(at + PHOTO_DRAFT_TTL) == 0
    with pytest.raises(KeyError):
        await photos.get(result.photo_source.id)


async def test_cleanup_forever_drains_backlog_and_preserves_protected_photos(test_database_url):
    photos, store = service(test_database_url)
    other, _ = service(test_database_url)
    expired_at = datetime.now(UTC) - PHOTO_DRAFT_TTL - timedelta(minutes=1)
    expired_ids = [uuid4() for _ in range(PHOTO_CLEANUP_BATCH_SIZE * 2 + 5)]
    retained_id = uuid4()
    referenced_id = uuid4()
    other_ids = [uuid4(), uuid4()]
    rows = [
        {
            "id": photo_id,
            "household_id": photos.household_id,
            "data": b"image",
            "mime_type": "image/png",
            "width": 1,
            "height": 1,
            "sha256": "0" * 64,
            "created_by": "actor",
            "created_at": expired_at,
            "last_draft_activity_at": expired_at,
            "retained_at": None,
        }
        for photo_id in expired_ids
    ]
    rows.extend(
        [
            {**rows[0], "id": retained_id, "retained_at": expired_at},
            {**rows[0], "id": referenced_id},
            *(
                {**rows[0], "id": photo_id, "household_id": other.household_id}
                for photo_id in other_ids
            ),
        ]
    )
    with store.connection() as connection:
        connection.execute(recipe_photos.insert(), rows)
    store.photo_household_id = photos.household_id
    store.save(
        "recipe",
        Recipe(
            id=uuid4(),
            **draft().model_dump(exclude={"photo_source"}),
            photo_source=PhotoSource(id=referenced_id),
        ),
    )

    batches = []
    drained = asyncio.Event()
    loop = asyncio.get_running_loop()
    cleanup = photos.cleanup

    def tracked_cleanup():
        deleted = cleanup()
        batches.append(deleted)
        if deleted < PHOTO_CLEANUP_BATCH_SIZE:
            loop.call_soon_threadsafe(drained.set)
        return deleted

    photos.cleanup = tracked_cleanup
    task = asyncio.create_task(photos.cleanup_forever(interval=3600))
    try:
        await asyncio.wait_for(drained.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert batches == [PHOTO_CLEANUP_BATCH_SIZE, PHOTO_CLEANUP_BATCH_SIZE, 5]
    with store.connection() as connection:
        remaining = set(
            connection.scalars(
                select(recipe_photos.c.id).where(
                    recipe_photos.c.household_id == photos.household_id
                )
            ).all()
        )
        other_remaining = set(
            connection.scalars(
                select(recipe_photos.c.id).where(recipe_photos.c.household_id == other.household_id)
            ).all()
        )
    assert remaining == {retained_id, referenced_id}
    assert other_remaining == set(other_ids)


async def test_redraft_refreshes_even_when_transcription_fails(test_database_url):
    photos, store = service(test_database_url)
    result = await upload(photos)
    old = datetime.now(UTC) - timedelta(hours=23)
    age(store, result.photo_source.id, old)
    photos.agent.run.side_effect = RuntimeError("provider failed")
    assert (await photos.redraft(result.photo_source.id, "actor")).status == "extraction_failed"
    stored = await photos.get(result.photo_source.id)
    assert stored.last_draft_activity_at > old + timedelta(hours=22)
    assert photos.cleanup(old + PHOTO_DRAFT_TTL) == 0


async def test_discard_is_scoped_and_never_removes_saved_photo(test_database_url):
    photos, store = service(test_database_url)
    other, _ = service(test_database_url)
    result = await upload(photos)
    with pytest.raises(KeyError):
        await other.discard(result.photo_source.id, "actor")
    with pytest.raises(KeyError):
        await other.redraft(result.photo_source.id, "actor")
    store.photo_household_id = other.household_id
    recipe = Recipe(id=uuid4(), **result.draft.model_dump())
    with pytest.raises(ValueError, match="not found"):
        store.save("recipe", recipe)
    store.photo_household_id = photos.household_id
    store.save("recipe", recipe)
    assert not await photos.discard(result.photo_source.id, "actor")
    assert photos.cleanup(datetime.now(UTC) + timedelta(days=7)) == 0
    # A previously saved source remains protected after recipe detachment/deletion.
    with store.connection() as connection:
        connection.execute(recipes.delete().where(recipes.c.id == recipe.id))
    assert not await photos.discard(result.photo_source.id, "actor")
    assert photos.cleanup(datetime.now(UTC) + timedelta(days=7)) == 0
    assert (await photos.get(result.photo_source.id)).data
    abandoned = await upload(photos)
    assert await photos.discard(abandoned.photo_source.id, "actor")
    with pytest.raises(KeyError):
        await photos.get(abandoned.photo_source.id)


async def test_cleanup_failure_retries_without_exposing_error(monkeypatch, caplog):
    photos = Mock()
    # Exercise the real loop without a database dependency.
    from home_kitchen.recipes.photo_storage import PhotoRecipeService

    first, second = Event(), asyncio.Event()
    attempts = 0

    async def offload(function):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first.set()
            raise RuntimeError("secret DSN")
        second.set()

    photos._offload = offload
    task = asyncio.create_task(PhotoRecipeService.cleanup_forever(photos, interval=0.001))
    try:
        await asyncio.wait_for(second.wait(), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert first.is_set() and attempts >= 2
    assert "secret DSN" not in caplog.text
    assert "will retry" in caplog.text


async def test_cleanup_cancellation_survives_offloaded_failure():
    from home_kitchen.recipes.photo_storage import PhotoRecipeService

    photos = Mock()
    started, release = Event(), Event()

    def fail_after_release():
        started.set()
        assert release.wait(2)
        raise RuntimeError("database failed during shutdown")

    photos.cleanup = fail_after_release
    photos._offload = PhotoRecipeService._offload
    task = asyncio.create_task(PhotoRecipeService.cleanup_forever(photos, interval=3600))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
        if not task.done():
            task.cancel()


async def test_discard_wins_save_race_without_dangling_reference(test_database_url, monkeypatch):
    photos, store = service(test_database_url)
    result = await upload(photos)
    locked, release = Event(), Event()
    original = photos._delete_unreferenced

    def pause(connection, photo_id):
        locked.set()
        assert release.wait(5)
        return original(connection, photo_id)

    monkeypatch.setattr(photos, "_delete_unreferenced", pause)
    recipe = Recipe(id=uuid4(), **result.draft.model_dump())
    with ThreadPoolExecutor(max_workers=2) as pool:
        deletion = pool.submit(photos._discard, result.photo_source.id)
        assert locked.wait(5)
        saving = pool.submit(store.save, "recipe", recipe)
        release.set()
        assert deletion.result(timeout=5)
        with pytest.raises(ValueError, match="not found"):
            saving.result(timeout=5)
    with store.connection() as connection:
        assert connection.scalar(select(recipes.c.id).where(recipes.c.id == recipe.id)) is None


async def test_save_wins_cleanup_and_discard_race(test_database_url, monkeypatch):
    photos, store = service(test_database_url)
    result = await upload(photos)
    age(store, result.photo_source.id, datetime.now(UTC) - timedelta(days=2))
    locked, release = Event(), Event()
    original = HistoryRepository.record

    def pause(self, *args, **kwargs):
        locked.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(HistoryRepository, "record", pause)
    recipe = Recipe(id=uuid4(), **result.draft.model_dump())
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving = pool.submit(store.save, "recipe", recipe)
        assert locked.wait(5)
        assert photos.cleanup() == 0  # SKIP LOCKED leaves the in-flight save alone.
        deletion = pool.submit(photos._discard, result.photo_source.id)
        release.set()
        saving.result(timeout=5)
        assert not deletion.result(timeout=5)
    assert (await photos.get(result.photo_source.id)).retained_at is not None
