"""Pure in-memory unit tests for decoupled recipe collection, discovery, and image ports."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from home_kitchen.models import Ingredient, Recipe, RecipeSource
from home_kitchen.recipes.adapters.memory import (
    MemoryCollectionUnitOfWork,
    MemoryRecipeImageRepository,
)
from home_kitchen.recipes.adapters.oda import OdaCollectionProvider, OdaDiscoveryProvider
from home_kitchen.recipes.bootstrap import (
    create_in_memory_collection_service,
    create_in_memory_recipe_image_service,
)
from home_kitchen.recipes.collections import CollectionEntry, CollectionPage
from home_kitchen.recipes.discovery import (
    RecipeDiscoveryRegistry,
    RecipeDiscoveryRequest,
    build_discovery_context,
)
from home_kitchen.recipes.images import RecipeImage, RecipeImageSearch
from home_kitchen.recipes.models import EvidenceSnippet


@dataclass
class _FakeDraft:
    title: str
    servings: int
    ingredients: list[str]
    instructions: list[str]
    source_url: str
    image_url: str | None = None
    recipe_type: None = None
    prep_time_minutes: None = None
    prep_category: None = None
    guided_steps: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.guided_steps is None:
            self.guided_steps = []


class _FakeCollectionProvider:
    id = "oda"

    def __init__(self, pages: dict[tuple[str, int], CollectionPage] | None = None) -> None:
        self.pages = pages or {}
        self.current = 1
        self.lock_calls: list[tuple[object, str, object]] = []

    def generation(self, actor_id: str) -> object:
        return self.current

    async def page(self, actor_id: str, kind: str, page: int, size: int) -> CollectionPage:
        return self.pages.get((kind, page), CollectionPage(entries=[], has_more=False)).model_copy(
            deep=True
        )

    def validate_source(self, entry: CollectionEntry) -> None:
        OdaCollectionProvider.validate_source(self, entry)  # type: ignore[arg-type]

    def lock_generation(self, connection: object, actor_id: str, generation: object) -> None:
        self.lock_calls.append((connection, actor_id, generation))
        if generation != self.current:
            raise ValueError("Oda connection changed; preview the collections again")


class _FakeImageProvider:
    def __init__(self, results: list[RecipeImage]) -> None:
        self._results = results

    async def search(
        self, query: str, *, limit: int = 10, continuation: str | None = None
    ) -> RecipeImageSearch:
        return RecipeImageSearch(status="ok", results=self._results[:limit])


async def _settle(service, actor_id: str, job_id: UUID):
    for _ in range(200):
        view = await service.get(actor_id, job_id)
        if view.status not in {"previewing", "importing"}:
            return view
        await asyncio.sleep(0.01)
    raise AssertionError("Collection job did not settle")


@pytest.mark.parametrize(
    ("relative_path", "forbidden_prefixes"),
    [
        (
            "src/home_kitchen/recipes/collections.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.oda",
                "home_kitchen.history",
            ),
        ),
        (
            "src/home_kitchen/recipes/discovery.py",
            (
                "httpx",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.oda",
                "home_kitchen.settings",
            ),
        ),
        (
            "src/home_kitchen/recipes/images.py",
            (
                "sqlalchemy",
                "httpx",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.history",
            ),
        ),
    ],
)
def test_core_recipe_modules_have_no_top_level_adapter_or_monolith_imports(
    relative_path: str, forbidden_prefixes: tuple[str, ...]
) -> None:
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for mod in imported:
        assert not any(
            mod == prefix or mod.startswith(f"{prefix}.") for prefix in forbidden_prefixes
        ), f"{relative_path} still imports {mod}"


@pytest.mark.asyncio
async def test_in_memory_collection_service_imports_resolves_and_maps() -> None:
    url = "https://oda.com/no/recipes/731-tomato-soup/"
    provider = _FakeCollectionProvider(
        {
            ("liked", 1): CollectionPage(
                entries=[
                    CollectionEntry(
                        id="731",
                        provider="oda",
                        title="Tomato soup",
                        source_url=url,
                        badges=["Liked on Oda"],
                    )
                ],
                has_more=False,
            )
        }
    )
    resolved_prod_id = uuid4()
    mapped_calls: list[tuple[str, list[UUID]]] = []

    class FakeIdentityResolver:
        def resolve_recipe_identities(self, recipe: Recipe) -> Recipe:
            return recipe.model_copy(
                update={
                    "ingredients": [
                        ing.model_copy(update={"canonical_product_id": resolved_prod_id})
                        for ing in recipe.ingredients
                    ]
                }
            )

    class FakePostImportMapper:
        async def map_imported_recipes(self, actor_id: str, recipe_ids: list[UUID]) -> None:
            mapped_calls.append((actor_id, list(recipe_ids)))

    async def extract(source_url: str) -> _FakeDraft:
        return _FakeDraft(
            title="Tomato soup",
            servings=2,
            ingredients=["200 g tomatoes"],
            instructions=["Simmer"],
            source_url=source_url,
        )

    service, uow = create_in_memory_collection_service(
        provider=provider,
        extractor=extract,
        identity_resolver=FakeIdentityResolver(),
        post_import_mapper=FakePostImportMapper(),
    )
    try:
        preview = await service.start_preview("alice", ["liked"])
        ready = await _settle(service, "alice", preview.job_id)
        assert ready.status == "ready"
        assert ready.entries[0].status == "pending"

        with pytest.raises(ValueError, match="Review"):
            await service.start_import("alice", preview.job_id, ["731"], reviewed=False)

        await service.start_import("alice", preview.job_id, ["731"], reviewed=True)
        done = await _settle(service, "alice", preview.job_id)
        assert done.status == "completed"
        assert done.entries[0].status == "imported"
        saved_id = done.entries[0].recipe_id
        assert saved_id is not None

        saved = uow.recipes[saved_id]
        assert saved.ingredients[0].canonical_product_id == resolved_prod_id
        assert ("oda", "731") in uow.source_links
        assert len(uow.history_entries) == 1
        assert uow.lock_calls == [("oda", "731")]
        assert mapped_calls == [("alice", [saved_id])]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_in_memory_collection_service_rolls_back_on_history_failure() -> None:
    url = "https://oda.com/no/recipes/731-tomato-soup/"
    provider = _FakeCollectionProvider(
        {
            ("liked", 1): CollectionPage(
                entries=[
                    CollectionEntry(
                        id="731",
                        provider="oda",
                        title="Tomato soup",
                        source_url=url,
                    )
                ],
                has_more=False,
            )
        }
    )
    uow = MemoryCollectionUnitOfWork()

    def fail_history(_entry: dict) -> None:
        raise RuntimeError("history unavailable")

    uow.history_hook = fail_history

    async def extract(source_url: str) -> _FakeDraft:
        return _FakeDraft(
            title="Tomato soup",
            servings=2,
            ingredients=["200 g tomatoes"],
            instructions=["Simmer"],
            source_url=source_url,
        )

    service, _ = create_in_memory_collection_service(uow, provider=provider, extractor=extract)
    try:
        preview = await service.start_preview("alice", ["liked"])
        await _settle(service, "alice", preview.job_id)
        await service.start_import("alice", preview.job_id, ["731"], reviewed=True)
        done = await _settle(service, "alice", preview.job_id)
        assert done.entries[0].status == "failed"
        assert uow.recipes == {}
        assert uow.source_links == {}
        assert uow.history_entries == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_in_memory_recipe_image_service_enforces_review_and_persists_attribution() -> None:
    recipe = Recipe(
        id=uuid4(),
        title="Fish soup",
        servings=4,
        ingredients=[Ingredient(name="cod", quantity=None, unit=None)],
        instructions=["Simmer"],
        source=RecipeSource.MANUAL,
    )
    candidate = RecipeImage(
        provider_id="42",
        query="fish soup",
        title="File:Fish_soup.jpg",
        image_url="https://upload.wikimedia.org/wikipedia/commons/a/ab/Fish_soup.jpg",
        thumbnail_url="https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Fish_soup.jpg/640px-Fish_soup.jpg",
        source_page_url="https://commons.wikimedia.org/wiki/File:Fish_soup.jpg",
        license_name="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution_required=True,
        attribution="Chef Photographer",
        obligations=["Credit the creator and link the license."],
    )
    service, repo = create_in_memory_recipe_image_service(
        MemoryRecipeImageRepository([recipe]),
        provider=_FakeImageProvider([candidate]),
    )

    session = await service.search("alice", "fish soup")
    assert session.search.status == "ok"

    with pytest.raises(ValueError, match="Review"):
        service.select("alice", recipe.id, session.search_id, "42", reviewed=False)

    with pytest.raises(KeyError):
        service.select("bob", recipe.id, session.search_id, "42", reviewed=True)

    with pytest.raises(KeyError):
        service.select("alice", uuid4(), session.search_id, "42", reviewed=True)

    updated = service.select("alice", recipe.id, session.search_id, "42", reviewed=True)
    assert str(updated.image_url) == candidate.image_url
    assert updated.image_attribution is not None
    assert updated.image_attribution.attribution == "Chef Photographer"
    assert len(repo.history_entries) == 1
    assert repo.history_entries[0]["recipe_id"] == recipe.id


@pytest.mark.asyncio
async def test_discovery_and_oda_providers_accept_narrow_structural_ports() -> None:
    prefs = SimpleNamespace(dietary_preferences=["no shellfish", "pescatarian"])
    context = build_discovery_context(
        prefs,
        [
            EvidenceSnippet(
                evidence_id="ev-1",
                text='{"kind":"feedback","message":"Loved salmon soup","structured":{"rating":5,"recipe_title":"salmon soup"}}',
            )
        ],
    )
    assert context.dietary_preferences == ["no shellfish", "pescatarian"]
    assert context.likes == ["salmon soup"]

    class FakeOdaSearchClient:
        def status(self, actor_id: str) -> dict:
            return {"connected": True}

        async def search_recipes(self, actor_id: str, query: str, page: int, size: int) -> dict:
            return {
                "recipes": [
                    {
                        "id": 10,
                        "title": "Salmon soup",
                        "url": "https://oda.com/no/recipes/10-salmon-soup/",
                    }
                ]
            }

    registry = RecipeDiscoveryRegistry([OdaDiscoveryProvider(FakeOdaSearchClient())])
    response = await registry.search(
        RecipeDiscoveryRequest(query="soup", sources=["oda"]),
        actor_id="alice",
        context=context,
    )
    assert len(response.results) == 1
    assert response.results[0].title == "Salmon soup"

    lock_calls: list[tuple[object, str, object]] = []

    class FakeOdaCollectionClient:
        def search_cache_context(self, actor_id: str) -> int:
            return 7

        def status(self, actor_id: str) -> dict:
            return {"connected": True}

        async def list_liked_recipes(self, actor_id: str, page: int = 1, size: int = 10) -> dict:
            return {"hasMore": False, "recipes": []}

        async def list_purchased_recipes(
            self, actor_id: str, page: int = 1, size: int = 10
        ) -> dict:
            return {"hasMore": False, "recipes": []}

    class FakeLocker:
        def lock_generation(self, connection: object, actor_id: str, generation: object) -> None:
            lock_calls.append((connection, actor_id, generation))

    oda_collections = OdaCollectionProvider(
        FakeOdaCollectionClient(),
        generation_locker=FakeLocker(),
    )
    assert oda_collections.generation("alice") == 7
    oda_collections.lock_generation("conn-token", "alice", 7)
    assert lock_calls == [("conn-token", "alice", 7)]


def test_compatibility_reexports_resolve_adapters_and_tables() -> None:
    from home_kitchen.recipes import collections, discovery, images

    assert collections.OdaCollectionProvider is OdaCollectionProvider
    assert collections.source_links.name == "recipe_source_links"
    assert discovery.OdaDiscoveryProvider is OdaDiscoveryProvider
    assert discovery.MarmitonDiscoveryProvider.__name__ == "MarmitonDiscoveryProvider"
    assert discovery.MatpratDiscoveryProvider.__name__ == "MatpratDiscoveryProvider"
    assert images.WikimediaCommonsImageProvider.__name__ == "WikimediaCommonsImageProvider"
    assert images.COMMONS_API.startswith("https://")


def test_postgres_recipe_image_repository_persists_with_sqlite_engine(tmp_path: Path) -> None:
    from sqlalchemy import create_engine, text

    from home_kitchen.models import RecipeImageAttribution
    from home_kitchen.recipes.adapters.postgres import PostgresRecipeImageRepository
    from home_kitchen.store import entity_values, recipes

    engine = create_engine(
        f"sqlite:///{tmp_path / 'images.db'}", connect_args={"check_same_thread": False}
    )
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE recipes (
                        id TEXT PRIMARY KEY,
                        position INTEGER,
                        title TEXT NOT NULL,
                        servings INTEGER NOT NULL,
                        ingredients TEXT NOT NULL,
                        instructions TEXT NOT NULL,
                        source_url TEXT,
                        image_url TEXT,
                        source TEXT NOT NULL,
                        source_ingredients TEXT NOT NULL DEFAULT '[]',
                        provenance TEXT,
                        photo_source TEXT,
                        photo_source_id TEXT,
                        image_attribution TEXT,
                        source_language TEXT NOT NULL DEFAULT 'und',
                        recipe_type TEXT,
                        prep_time_minutes INTEGER,
                        prep_category TEXT,
                        guided_steps TEXT NOT NULL DEFAULT '[]'
                    )
                    """
                )
            )
        recipe = Recipe(
            id=uuid4(),
            title="Tomato soup",
            servings=2,
            ingredients=[Ingredient(name="tomatoes", quantity=None, unit=None)],
            instructions=["Simmer"],
            source=RecipeSource.MANUAL,
        )
        with engine.begin() as conn:
            conn.execute(recipes.insert().values(**entity_values(recipe)))

        recorded: list[tuple[object, str | None, object]] = []
        history = SimpleNamespace(
            record=lambda data, actor_id=None, connection=None: recorded.append(
                (data, actor_id, connection)
            )
        )
        repo = PostgresRecipeImageRepository(engine, history)
        attribution = RecipeImageAttribution(
            provider="wikimedia_commons",
            provider_id="42",
            query="tomato soup",
            title="File:Soup.jpg",
            image_url="https://upload.wikimedia.org/wikipedia/commons/a/ab/Soup.jpg",
            source_page_url="https://commons.wikimedia.org/wiki/File:Soup.jpg",
            license_name="CC BY 4.0",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            attribution_required=True,
            attribution="Chef Photographer",
            obligations=["Credit the creator and link the license."],
        )

        with pytest.raises(KeyError):
            repo.save_selected_image(
                actor_id="alice",
                recipe_id=uuid4(),
                image_url=str(attribution.image_url),
                attribution=attribution,
                provider=attribution.provider,
                provider_id=attribution.provider_id,
                source_page_url=str(attribution.source_page_url),
                license_name=attribution.license_name,
                license_url=str(attribution.license_url),
            )

        updated = repo.save_selected_image(
            actor_id="alice",
            recipe_id=recipe.id,
            image_url=str(attribution.image_url),
            attribution=attribution,
            provider=attribution.provider,
            provider_id=attribution.provider_id,
            source_page_url=str(attribution.source_page_url),
            license_name=attribution.license_name,
            license_url=str(attribution.license_url),
        )
        assert updated.image_url == attribution.image_url
        assert updated.image_attribution is not None
        assert updated.image_attribution.attribution == "Chef Photographer"
        assert len(recorded) == 1
        assert recorded[0][1] == "alice"
        assert recorded[0][2] is not None
    finally:
        engine.dispose()
