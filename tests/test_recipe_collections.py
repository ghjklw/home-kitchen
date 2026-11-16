"""Collection import uses fake provider reads and public extraction only."""

import asyncio
import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from home_kitchen.history import HistoryRepository
from home_kitchen.imports import ImportedDraft
from home_kitchen.inventory import InventoryService
from home_kitchen.models import MealCreate, RecipeCreate
from home_kitchen.recipes.collections import (
    CollectionEntry,
    CollectionPage,
    CollectionService,
    OdaCollectionProvider,
    source_ingredient,
    source_links,
)
from home_kitchen.service import KitchenService

URL = "https://oda.com/no/recipes/731-tomato-soup/"


class Provider:
    id = "oda"
    current = 1

    def __init__(self, pages=None):
        self.pages = pages or {}
        self.calls = []

    def generation(self, actor):
        return self.current

    async def page(self, actor, kind, page, size):
        self.calls.append((actor, kind, page, size))
        return self.pages.get((kind, page), CollectionPage(entries=[], has_more=False)).model_copy(
            deep=True
        )

    def lock_generation(self, connection, actor, generation):
        if self.current != generation:
            raise ValueError("Connection changed")

    def validate_source(self, entry):
        OdaCollectionProvider.validate_source(self, entry)


def entry(id="731", url=URL, badge="Liked on Oda"):
    return CollectionEntry(
        id=id, provider="oda", title="Tomato soup", source_url=url, badges=[badge]
    )


def draft(url=URL):
    return ImportedDraft(
        title="Soup",
        servings=2,
        ingredients=["200 g tomatoes", "salt to taste", "1/2 onion"],
        instructions=["Simmer"],
        source_url=url,
    )


async def settle(service, actor, id):
    for _ in range(500):
        view = await service.get(actor, id)
        if view.status not in {"previewing", "importing"}:
            return view
        await asyncio.sleep(0.01)
    pytest.fail("job did not finish")


@pytest.fixture
def kitchen(test_database_url):
    result = KitchenService(test_database_url)
    yield result
    result.store.engine.dispose()


def make_service(kitchen, provider, extractor=None):
    async def extract(url):
        return draft(url)

    return CollectionService(
        kitchen, HistoryRepository(kitchen.store.engine), provider, extractor or extract
    )


@pytest.mark.asyncio
async def test_pagination_dedup_missing_urls_badges_and_actor_isolation(kitchen):
    provider = Provider(
        {
            ("liked", 1): CollectionPage(entries=[entry()], has_more=True),
            ("liked", 2): CollectionPage(entries=[entry("732", None)], has_more=False),
            ("purchased", 1): CollectionPage(
                entries=[entry(badge="Ingredients bought on Oda")], has_more=False
            ),
        }
    )
    service = make_service(kitchen, provider)
    try:
        job = await service.start_preview("alice", ["liked", "purchased"])
        with pytest.raises(KeyError):
            await service.get("bob", job.job_id)
        view = await settle(service, "alice", job.job_id)
        assert view.status == "ready"
        assert [item.id for item in view.entries] == ["731", "732"]
        assert view.entries[0].badges == ["Liked on Oda", "Ingredients bought on Oda"]
        assert view.entries[1].status == "missing_url"
        assert provider.calls == [
            ("alice", "liked", 1, 20),
            ("alice", "liked", 2, 20),
            ("alice", "purchased", 1, 20),
        ]
        assert kitchen.list_recipes() == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_generation_checks_run_outside_event_loop(kitchen):
    loop_thread = threading.get_ident()

    class ThreadCheckingProvider(Provider):
        def __init__(self):
            super().__init__()
            self.generation_threads = []

        def generation(self, actor):
            self.generation_threads.append(threading.get_ident())
            return super().generation(actor)

    provider = ThreadCheckingProvider()
    service = make_service(kitchen, provider)
    try:
        job = await service.start_preview("alice", ["liked"])
        await settle(service, "alice", job.job_id)
        assert provider.generation_threads
        assert all(thread_id != loop_thread for thread_id in provider.generation_threads)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_exact_confirmation_atomic_import_and_retry_keep_adaptations(kitchen):
    provider = Provider({("liked", 1): CollectionPage(entries=[entry()], has_more=False)})
    calls = []

    async def extract(url):
        calls.append(url)
        return draft(url)

    service = make_service(kitchen, provider, extract)
    try:
        job = await service.start_preview("alice", ["liked"])
        await settle(service, "alice", job.job_id)
        for ids, reviewed in [
            (["731"], False),
            (["all"], True),
            (["731", "731"], True),
            ([], True),
        ]:
            with pytest.raises(ValueError):
                await service.start_import("alice", job.job_id, ids, reviewed)
        await service.start_import("alice", job.job_id, ["731"], True)
        view = await settle(service, "alice", job.job_id)
        assert view.entries[0].status == "imported"
        saved = kitchen.list_recipes()[0]
        assert calls == [URL]
        assert saved.source == "imported"
        assert str(saved.source_url) == URL
        assert saved.ingredients[0].quantity == 200
        assert saved.ingredients[1].quantity is None
        assert saved.ingredients[2].quantity is None
        assert saved.source_ingredients[1].text == "salt to taste"
        assert saved.ingredients[1].source_indices == [1]
        assert len(service.history.list()) == 1
        changed = RecipeCreate(**saved.model_dump(exclude={"id"}))
        changed.title = "My adapted soup"
        kitchen.update_recipe(saved.id, changed)
        await service.start_import("alice", job.job_id, ["731"], True)
        await settle(service, "alice", job.job_id)
        new = await service.start_preview("alice", ["liked"])
        refreshed = await settle(service, "alice", new.job_id)
        assert refreshed.entries[0].status == "already_imported"
        assert kitchen.get_recipe(saved.id).title == "My adapted soup"
        assert len(kitchen.list_recipes()) == 1
        assert calls == [URL]
        # A durable identity still identifies the saved recipe when Oda later
        # omits its public URL; no null-URL household recipes are matched.
        provider.pages[("liked", 1)].entries[0].source_url = None
        missing_url_job = await service.start_preview("alice", ["liked"])
        missing_url_view = await settle(service, "alice", missing_url_job.job_id)
        assert missing_url_view.entries[0].status == "already_imported"
        assert missing_url_view.entries[0].recipe_id == saved.id
        with kitchen.store.engine.connect() as connection:
            assert connection.execute(select(source_links.c.recipe_id)).scalar_one() == saved.id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_partial_failure_retry_and_cap(kitchen):
    class Endless(Provider):
        async def page(self, actor, kind, page, size):
            return CollectionPage(
                entries=[entry(str(page), f"https://oda.com/no/recipes/{page}-soup/")],
                has_more=True,
            )

    provider = Endless()
    failures = {"https://oda.com/no/recipes/1-soup/"}

    async def extract(url):
        if url in failures:
            raise ValueError("private details must not leak")
        return draft(url)

    service = make_service(kitchen, provider, extract)
    try:
        job = await service.start_preview("alice", ["liked"])
        view = await settle(service, "alice", job.job_id)
        assert view.pages_read == 50 and view.truncated
        assert view.truncated_kinds == ["liked"]
        await service.start_import("alice", job.job_id, ["1", "2"], True)
        view = await settle(service, "alice", job.job_id)
        assert [item.status for item in view.entries[:2]] == ["failed", "imported"]
        assert "private" not in view.entries[0].error
        failures.clear()
        await service.start_import("alice", job.job_id, ["1"], True)
        view = await settle(service, "alice", job.job_id)
        assert view.entries[0].status == "imported"
        assert len(kitchen.list_recipes()) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancel_and_generation_change_never_save_inflight(kitchen):
    began, release = asyncio.Event(), asyncio.Event()

    async def extract(url):
        began.set()
        await release.wait()
        return draft(url)

    provider = Provider({("liked", 1): CollectionPage(entries=[entry()], has_more=False)})
    service = make_service(kitchen, provider, extract)
    try:
        job = await service.start_preview("alice", ["liked"])
        await settle(service, "alice", job.job_id)
        await service.start_import("alice", job.job_id, ["731"], True)
        await began.wait()
        await service.cancel("alice", job.job_id)
        assert (await service.get("alice", job.job_id)).status == "cancelled"
        assert kitchen.list_recipes() == []
        new = await service.start_preview("alice", ["liked"])
        await settle(service, "alice", new.job_id)
        provider.current = 2
        with pytest.raises(ValueError, match="connection changed"):
            await service.start_import("alice", new.job_id, ["731"], True)
    finally:
        await service.close()


@pytest.mark.parametrize(
    "raw", ["salt", "1/2 onion", "2-3 g salt", "about 2 g salt", "0 g salt", "2 unknown salt"]
)
def test_ambiguous_quantities_remain_raw_and_unquantified(raw):
    ingredient = source_ingredient(raw, 0)
    assert ingredient.name == ("salt" if raw == "0 g salt" else raw)
    assert ingredient.quantity is None and ingredient.unit is None


def test_oda_identity_requires_exact_supplied_source():
    provider = Provider()
    for invalid in [
        entry("1"),
        entry(url="https://matprat.no/no/recipes/731-tomato-soup/"),
        entry(url=URL + "?a=1"),
    ]:
        with pytest.raises(ValueError):
            provider.validate_source(invalid)


@pytest.mark.asyncio
async def test_exact_saved_url_links_without_extraction_or_overwriting(kitchen):
    saved = kitchen.create_recipe(
        RecipeCreate(
            title="Previously adapted",
            servings=3,
            ingredients=[source_ingredient("salt to taste", 0)],
            source_ingredients=[{"index": 0, "text": "salt to taste"}],
            instructions=["Enjoy"],
            source_url=URL,
        )
    )

    async def no_extract(url):
        pytest.fail("Existing source must not be extracted")

    provider = Provider({("liked", 1): CollectionPage(entries=[entry()], has_more=False)})
    service = make_service(kitchen, provider, no_extract)
    try:
        job = await service.start_preview("alice", ["liked"])
        view = await settle(service, "alice", job.job_id)
        assert view.entries[0].status == "already_imported"
        await service.start_import("alice", job.job_id, ["731"], True)
        view = await settle(service, "alice", job.job_id)
        assert view.entries[0].recipe_id == saved.id
        assert kitchen.get_recipe(saved.id).title == "Previously adapted"
        with kitchen.store.engine.connect() as connection:
            assert connection.execute(select(source_links.c.recipe_id)).scalar_one() == saved.id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_history_failure_rolls_back_recipe_and_mapping(kitchen, monkeypatch):
    provider = Provider({("liked", 1): CollectionPage(entries=[entry()], has_more=False)})
    service = make_service(kitchen, provider)
    original_record = service.history.record

    def fail(*args, **kwargs):
        raise ValueError("Simulated history failure")

    try:
        job = await service.start_preview("alice", ["liked"])
        await settle(service, "alice", job.job_id)
        monkeypatch.setattr(service.history, "record", fail)
        await service.start_import("alice", job.job_id, ["731"], True)
        view = await settle(service, "alice", job.job_id)
        assert view.entries[0].status == "failed"
        assert kitchen.list_recipes() == []
        with kitchen.store.engine.connect() as connection:
            assert connection.execute(select(source_links.c.recipe_id)).first() is None
        monkeypatch.setattr(service.history, "record", original_record)
        await service.start_import("alice", job.job_id, ["731"], True)
        view = await settle(service, "alice", job.job_id)
        assert view.entries[0].status == "imported"
        assert len(kitchen.list_recipes()) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_job_limit_expiration_and_restart_message(kitchen):
    provider = Provider()
    service = make_service(kitchen, provider)
    service.MAX_JOBS = 1
    try:
        job = await service.start_preview("alice", ["liked"])
        await settle(service, "alice", job.job_id)
        with pytest.raises(ValueError, match="Too many"):
            await service.start_preview("alice", ["liked"])
        service._jobs[job.job_id].created -= service.TTL_SECONDS + 1
        with pytest.raises(KeyError, match="preview.*again"):
            await service.get("alice", job.job_id)
        fresh = await service.start_preview("alice", ["liked"])
        assert fresh.job_id != job.job_id
        restarted = make_service(kitchen, provider)
        with pytest.raises(KeyError, match="Saved recipes are retained"):
            await restarted.get("alice", fresh.job_id)
        await restarted.close()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_job_limit_reserves_capacity_while_provider_context_loads(kitchen):
    class SlowGeneration(Provider):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def generation(self, actor):
            self.started.set()
            if not self.release.wait(2):
                raise TimeoutError("test generation barrier timed out")
            return self.current

    provider = SlowGeneration()
    service = make_service(kitchen, provider)
    service.MAX_JOBS = 1
    first = asyncio.create_task(service.start_preview("alice", ["liked"]))
    try:
        assert await asyncio.to_thread(provider.started.wait, 1)
        with pytest.raises(ValueError, match="Too many"):
            await service.start_preview("bob", ["liked"])
        provider.release.set()
        accepted = await first
        assert len(service._jobs) == 1
        await settle(service, "alice", accepted.job_id)
    finally:
        provider.release.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_concurrent_jobs_share_exact_source_recipe(kitchen):
    arrived = 0
    both = asyncio.Event()

    async def extract(url):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await both.wait()
        return draft(url)

    provider = Provider({("liked", 1): CollectionPage(entries=[entry()], has_more=False)})
    service = make_service(kitchen, provider, extract)
    try:
        jobs = [await service.start_preview(actor, ["liked"]) for actor in ("alice", "bob")]
        for actor, job in zip(("alice", "bob"), jobs, strict=True):
            await settle(service, actor, job.job_id)
            await service.start_import(actor, job.job_id, ["731"], True)
        results = [
            await settle(service, actor, job.job_id)
            for actor, job in zip(("alice", "bob"), jobs, strict=True)
        ]
        assert len(kitchen.list_recipes()) == 1
        assert results[0].entries[0].recipe_id == results[1].entries[0].recipe_id
        assert len(service.history.list()) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_oda_collection_estimate_is_separate_and_used_by_planned_shopping(kitchen):
    imported = ImportedDraft(
        title="Sukkerkake",
        servings=2,
        ingredients=["1 dl sukker", "1 klype salt"],
        instructions=["Rør sammen"],
        source_url=URL,
    )

    async def extract(url):
        return imported

    provider = Provider({("liked", 1): CollectionPage(entries=[entry()], has_more=False)})
    service = make_service(kitchen, provider, extract)
    try:
        job = await service.start_preview("alice", ["liked"])
        await settle(service, "alice", job.job_id)
        await service.start_import("alice", job.job_id, ["731"], True)
        await settle(service, "alice", job.job_id)

        saved = kitchen.list_recipes()[0]
        assert [(item.quantity, item.unit) for item in saved.ingredients] == [
            (Decimal("1"), "dl"),
            (Decimal("1"), "klype"),
        ]
        assert saved.ingredients[0].purchase_estimate.quantity == Decimal("90")
        assert saved.ingredients[0].purchase_estimate.unit == "g"
        assert saved.ingredients[1].purchase_estimate is None
        assert [item.text for item in saved.source_ingredients] == imported.ingredients

        kitchen.plan_meal(MealCreate(date=date(2026, 11, 16), recipe_id=saved.id, servings=2))
        shopping = kitchen.shopping_list(
            date(2026, 11, 16), date(2026, 11, 22), today=date(2026, 11, 16)
        )
        assert [(line.name, line.quantity, line.unit) for line in shopping.lines] == [
            ("sukker", Decimal("90"), "g")
        ]
        assert [(need.name, need.unit) for need in shopping.unquantified] == [("salt", "klype")]
        purchase = (
            InventoryService(kitchen).plan(date(2026, 11, 16), date(2026, 11, 22)).purchase_lines[0]
        )
        assert purchase.approximate is True
        assert "1 dl sukker" in purchase.provenance
        assert str(purchase.source_url).startswith("https://www.matprat.no/")
    finally:
        await service.close()
