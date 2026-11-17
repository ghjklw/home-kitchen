"""Offline unit and transport tests for Cookidoo recipe collection preview and import.

Uses httpx.MockTransport and SQLite in-memory storage; zero network calls or
external PostgreSQL database required.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from fastmcp import Client
from pydantic import SecretStr
from sqlalchemy import create_engine, select, text

from home_kitchen.basket.repository import BasketService
from home_kitchen.connector_auth import ConnectorAuthService
from home_kitchen.connector_store import ConnectorStore
from home_kitchen.connector_store import metadata as connector_metadata
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.cookidoo_recipes import (
    CookidooCollectionProvider,
    CookidooError,
    CookidooRecipeService,
    normalize_cookidoo_recipe,
)
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.integrations import CookidooIntegration, IntegrationRegistry, OdaIntegration
from home_kitchen.mcp_server import build_mcp
from home_kitchen.models import RecipeSource
from home_kitchen.recipes.collections import (
    CollectionEntry,
    CollectionPage,
    CollectionService,
    source_links,
)
from home_kitchen.settings import Household, Settings
from home_kitchen.store import recipes


@pytest.fixture
def sqlite_engine(tmp_path):
    db_path = tmp_path / "cookidoo_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    connector_metadata.create_all(engine)
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
                    source_language TEXT NOT NULL DEFAULT 'und'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE recipe_source_links (
                    provider TEXT NOT NULL,
                    external_recipe_id TEXT NOT NULL,
                    recipe_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (provider, external_recipe_id)
                )
                """
            )
        )
    yield engine
    engine.dispose()


@pytest.fixture
def cipher():
    return Fernet(Fernet.generate_key())


@pytest.fixture
def connector_store(sqlite_engine, cipher):
    return ConnectorStore(sqlite_engine, cipher)


class FakeHistory:
    def __init__(self):
        self.entries = []

    def record(self, data, actor_id=None, connection=None):
        self.entries.append((data, actor_id))
        return data


def _save_cookidoo_connection(
    store: ConnectorStore,
    household_id: UUID,
    *,
    access_token: str | None = "cookidoo-access-token-123456",
    session_cookie: str | None = None,
    country: str = "no",
    expires_at: datetime | None = None,
):
    creds: dict[str, str] = {"country": country}
    if access_token is not None:
        creds["access_token"] = access_token
        creds["token_type"] = "Bearer"
    if session_cookie is not None:
        creds["session_cookie"] = session_cookie
    store.save_connection(
        household_id=household_id,
        provider_id="cookidoo",
        auth_mode="oauth_password" if access_token else "session_cookie",
        credentials=creds,
        expires_at=expires_at,
        actor_id="malo",
    )


def _offline_app(
    sqlite_engine,
    cipher,
    *,
    household_id: UUID,
    cookidoo_transport: httpx.AsyncBaseTransport,
    oda_provider=None,
    actor_id: str = "malo",
):
    store = ConnectorStore(sqlite_engine, cipher)
    history = FakeHistory()
    kitchen = SimpleNamespace(store=SimpleNamespace(engine=sqlite_engine))

    class FakeOda:
        def status(self, actor):
            return {"connected": False, "configured": False}

        def disconnect(self, actor):
            pass

    class FakeOdaProvider:
        id = "oda"

        def generation(self, actor):
            return 1

        async def page(self, actor, kind, page, size):
            return CollectionPage(
                entries=[
                    CollectionEntry(
                        id="731",
                        provider="oda",
                        title="Oda Tomatsuppe",
                        source_url="https://oda.com/no/recipes/731-tomato-soup/",
                    )
                ]
                if page == 1
                else [],
                has_more=False,
            )

        def lock_generation(self, connection, actor, generation):
            assert generation == 1

        def validate_source(self, entry):
            pass

    settings = Settings(
        local_dev=True,
        database_url=SecretStr("postgresql+psycopg://user:pwd@localhost/db"),
    )
    integrations = IntegrationRegistry(
        [
            OdaIntegration(FakeOda()),
            CookidooIntegration(store, household_id),
        ]
    )
    cookidoo_provider = CookidooCollectionProvider(
        store, household_id, transport=cookidoo_transport
    )
    collection_service = CollectionService(
        kitchen,
        history,
        providers={
            "oda": oda_provider or FakeOdaProvider(),
            "cookidoo": cookidoo_provider,
        },
    )
    features = SimpleNamespace(
        integrations=integrations,
        connector_store=store,
        connector_auth=ConnectorAuthService(
            store=store,
            integrations=integrations,
            household_id=household_id,
            settings=settings,
        ),
        household_id=household_id,
        oda=FakeOda(),
        settings=settings,
        recipe_collections=collection_service,
        # Unused by collection endpoints/tools, present for router/MCP wiring
        recipe_photos=SimpleNamespace(),
        recipe_images=SimpleNamespace(),
        basket_jobs=SimpleNamespace(),
        plan_proposals=SimpleNamespace(),
        inventory=SimpleNamespace(),
        purchase_modes=SimpleNamespace(),
        purchase_sync=SimpleNamespace(),
        store_trip=SimpleNamespace(),
        trip_completion=SimpleNamespace(),
        catalogue=SimpleNamespace(),
        catalogue_identities=SimpleNamespace(),
        catalogue_skus=SimpleNamespace(),
        product_overview=SimpleNamespace(),
        history=history,
        recipe_feedback=SimpleNamespace(),
        recipe_providers=SimpleNamespace(),
        recipe_discovery=SimpleNamespace(),
        oda_cart=SimpleNamespace(),
    )
    basket = BasketService(kitchen, history)
    mcp = build_mcp(SimpleNamespace(store=kitchen.store), lambda: Household())
    install_feature_tools(mcp, features, basket)

    app = FastAPI()

    @app.middleware("http")
    async def inject_actor(request, call_next):
        request.state.actor_id = request.headers.get("x-test-actor", actor_id)
        return await call_next(request)

    @app.exception_handler(KeyError)
    async def missing(_request, _exc):
        return JSONResponse({"detail": "Item not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    app.include_router(feature_router(features, basket))
    return app, features, history, store


def _settled(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        resp = client.get(f"/api/recipe-collections/{job_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] not in {"previewing", "importing"}:
            return body
        time.sleep(0.01)
    pytest.fail("collection job did not settle")


def test_normalize_cookidoo_recipe_handles_variants_and_sanitizes_images():
    entry = normalize_cookidoo_recipe(
        {
            "recipeId": "r12345",
            "recipeName": "Kremet blomkålsuppe",
            "yield": {"value": "6"},
            "imageUrl": "/images/r12345.jpg",
            "recipeIngredients": [
                {"text": "500 g blomkål"},
                "2 dl fløte",
                {"name": "1 ts salt"},
            ],
            "recipeInstructions": [
                {"description": "Kutt blomkål i buketter."},
                "Kjør glatt i Thermomix på hastighet 10.",
            ],
            "prepTime": 600,
            "totalTime": 1800,
            "tools": ["TM6"],
        },
        country="no",
    )
    assert entry.id == "r12345"
    assert entry.provider == "cookidoo"
    assert entry.title == "Kremet blomkålsuppe"
    assert entry.servings == 6
    assert entry.source_url == "https://cookidoo.no/recipes/recipe/nb-NO/r12345"
    assert entry.image_url == "https://cookidoo.no/images/r12345.jpg"
    assert entry.ingredients == ["500 g blomkål", "2 dl fløte", "1 ts salt"]
    assert entry.instructions == [
        "Kutt blomkål i buketter.",
        "Kjør glatt i Thermomix på hastighet 10.",
    ]
    assert entry.metadata["prep_time"] == 600
    assert entry.metadata["tools"] == ["TM6"]

    insecure = normalize_cookidoo_recipe(
        {"id": "r99", "title": "Suppe", "imageUrl": "http://insecure.example/img.jpg"}
    )
    assert insecure.image_url is None


def test_cookidoo_provider_credentials_fencing_and_url_validation(connector_store, sqlite_engine):
    hid = uuid4()
    provider = CookidooCollectionProvider(connector_store, hid)

    with pytest.raises(CookidooError) as exc_unconnected:
        provider.generation("malo")
    assert exc_unconnected.value.status_code == 503
    assert "ikke tilkoblet" in exc_unconnected.value.detail

    _save_cookidoo_connection(
        connector_store, hid, expires_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    with pytest.raises(CookidooError) as exc_expired:
        provider.generation("malo")
    assert exc_expired.value.status_code == 401
    assert "utløpt" in exc_expired.value.detail

    _save_cookidoo_connection(connector_store, hid, access_token="   ")
    with pytest.raises(CookidooError) as exc_invalid:
        provider.generation("malo")
    assert exc_invalid.value.status_code == 401

    _save_cookidoo_connection(connector_store, hid, access_token="valid-token-123456")
    gen = provider.generation("malo")
    with sqlite_engine.begin() as conn:
        provider.lock_generation(conn, "malo", gen)

    # Rotate token -> lock_generation fails
    _save_cookidoo_connection(connector_store, hid, access_token="rotated-token-999999")
    with sqlite_engine.begin() as conn:
        with pytest.raises(ValueError, match="endret"):
            provider.lock_generation(conn, "malo", gen)

    # Disconnect -> lock_generation fails
    connector_store.delete_connection(hid, "cookidoo")
    with sqlite_engine.begin() as conn:
        with pytest.raises(ValueError, match="fjernet"):
            provider.lock_generation(conn, "malo", gen)

    # URL validation
    for valid_url in (
        "https://cookidoo.no/recipes/recipe/nb-NO/r100",
        "https://cookidoo.de/recipes/recipe/de-DE/r200",
        "https://cookidoo.thermomix.com/recipes/recipe/en-US/r300",
        "https://cookidoo.no/created-recipes/nb-NO/c1",
    ):
        provider.validate_source(
            CollectionEntry(id="x", provider="cookidoo", title="T", source_url=valid_url)
        )

    for invalid_url in (
        None,
        "http://cookidoo.no/recipes/recipe/nb-NO/r100",
        "https://user:pass@cookidoo.no/recipes/recipe/nb-NO/r100",
        "https://cookidoo.evil.com/recipes/recipe/nb-NO/r100",
        "https://www.matprat.no/oppskrifter/123/",
        "https://cookidoo.no/about",
    ):
        with pytest.raises(ValueError):
            provider.validate_source(
                CollectionEntry(id="x", provider="cookidoo", title="T", source_url=invalid_url)
            )


@pytest.mark.asyncio
async def test_cookidoo_provider_page_mock_transport_fallbacks_and_errors(connector_store):
    hid = uuid4()
    _save_cookidoo_connection(
        connector_store, hid, access_token="tok-abc", session_cookie="v_sess=xyz"
    )
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        assert request.headers["authorization"] == "Bearer tok-abc"
        assert request.headers["cookie"] == "v_sess=xyz"

        if request.url.path == "/api/v2/user/recipes":
            return httpx.Response(404)
        if request.url.path == "/api/recipe-collections":
            return httpx.Response(
                200,
                json={
                    "collections": [
                        {
                            "name": "Favoritter",
                            "recipes": [
                                {
                                    "id": "r10",
                                    "title": "Fiskegrateng",
                                    "servings": 4,
                                    "ingredients": ["400 g torsk", "2 dl makaroni"],
                                    "instructions": ["Kok makaroni", "Stek i ovn"],
                                }
                            ],
                        }
                    ],
                    "total": 25,
                },
            )
        if request.url.path == "/created-recipes/nb-NO":
            return httpx.Response(
                200,
                json={
                    "createdRecipes": [
                        {
                            "id": "c1",
                            "title": "Egen vaffelrøre",
                            "url": "https://cookidoo.no/created-recipes/nb-NO/c1",
                        }
                    ],
                    "hasMore": False,
                },
            )
        if request.url.path == "/api/v2/user/bookmarks":
            return httpx.Response(401)
        return httpx.Response(500)

    provider = CookidooCollectionProvider(
        connector_store, hid, transport=httpx.MockTransport(handler)
    )

    saved_page = await provider.page("malo", "saved", 1, 20)
    assert saved_page.has_more is True
    assert len(saved_page.entries) == 1
    assert saved_page.entries[0].id == "r10"
    assert saved_page.entries[0].badges == ["Favoritter"]
    assert saved_page.entries[0].ingredients == ["400 g torsk", "2 dl makaroni"]

    created_page = await provider.page("malo", "created", 1, 20)
    assert created_page.has_more is False
    assert created_page.entries[0].id == "c1"
    assert created_page.entries[0].source_url == "https://cookidoo.no/created-recipes/nb-NO/c1"

    with pytest.raises(CookidooError) as auth_err:
        await provider.page("malo", "bookmarks", 1, 20)
    assert auth_err.value.status_code == 401
    assert "logg inn" in auth_err.value.detail.lower()

    with pytest.raises(CookidooError) as kind_err:
        await provider.page("malo", "unknown", 1, 20)
    assert kind_err.value.status_code == 400


@pytest.mark.asyncio
async def test_cookidoo_provider_network_error_returns_502(connector_store):
    hid = uuid4()
    _save_cookidoo_connection(connector_store, hid)

    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    provider = CookidooCollectionProvider(
        connector_store, hid, transport=httpx.MockTransport(failing_handler)
    )
    with pytest.raises(CookidooError) as exc:
        await provider.page("malo", "saved", 1, 20)
    assert exc.value.status_code == 502
    assert "Nettverksfeil" in exc.value.detail


def test_cookidoo_recipe_service_atomic_import_and_dedup(connector_store, sqlite_engine):
    hid = uuid4()
    _save_cookidoo_connection(connector_store, hid)
    kitchen = SimpleNamespace(store=SimpleNamespace(engine=sqlite_engine))
    history = FakeHistory()
    service = CookidooRecipeService(kitchen, history, connector_store, hid)

    entry = CollectionEntry(
        id="r555",
        provider="cookidoo",
        title="Tomatsuppe",
        source_url="https://cookidoo.no/recipes/recipe/nb-NO/r555",
        servings=4,
        ingredients=["400 g hakkede tomater", "1 løk"],
        instructions=["Fres løk", "Kok opp tomater"],
    )

    with pytest.raises(ValueError, match="Gjennomgå"):
        service.import_recipe_atomic("malo", entry, reviewed=False)

    recipe_id, existed = service.import_recipe_atomic("malo", entry, reviewed=True)
    assert existed is False
    assert len(history.entries) == 1
    assert history.entries[0][0].message == "Importerte Cookidoo-oppskrift"

    with sqlite_engine.connect() as conn:
        row = conn.execute(select(recipes)).one()
        assert row.source == RecipeSource.COOKIDOO
        assert row.source_url == "https://cookidoo.no/recipes/recipe/nb-NO/r555"
        assert row.instructions == ["Fres løk", "Kok opp tomater"]

    # Re-importing same entry returns existing ID without overwriting
    second_id, existed_second = service.import_recipe_atomic("malo", entry, reviewed=True)
    assert existed_second is True
    assert second_id == recipe_id
    assert len(history.entries) == 1


def test_rest_api_cookidoo_preview_import_and_provider_isolation(sqlite_engine, cipher):
    hid = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/user/recipes":
            return httpx.Response(
                200,
                json={
                    "hasMore": False,
                    "recipes": [
                        {
                            "id": "r1",
                            "title": "Risotto med sopp",
                            "servings": 4,
                            "ingredients": ["300 g risottoris", "200 g sjampinjong"],
                            "instructions": ["Stek sopp", "Tilsett kraft litt etter litt"],
                        },
                        {
                            "id": "r2",
                            "title": "Pannekaker",
                            "url": "https://cookidoo.no/recipes/recipe/nb-NO/r2",
                        },
                    ],
                },
            )
        if request.url.path == "/created-recipes/nb-NO":
            return httpx.Response(
                200,
                json={
                    "hasMore": False,
                    "createdRecipes": [
                        {
                            "id": "r1",
                            "title": "Risotto med sopp",
                            "badges": ["Mine oppskrifter"],
                        }
                    ],
                },
            )
        if request.url.path == "/api/v2/recipes/r2":
            return httpx.Response(
                200,
                json={
                    "id": "r2",
                    "title": "Pannekaker",
                    "servings": 4,
                    "ingredients": ["3 dl hvetemel", "6 dl melk", "3 egg"],
                    "instructions": ["Visp sammen røren", "Stek gylne pannekaker"],
                },
            )
        return httpx.Response(404)

    app, features, history, store = _offline_app(
        sqlite_engine,
        cipher,
        household_id=hid,
        cookidoo_transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as client:
        # Unconnected Cookidoo returns 503 Bokmål guidance
        unconnected = client.post(
            "/api/recipe-collections",
            json={"provider": "cookidoo", "kinds": ["saved"]},
        )
        assert unconnected.status_code == 503
        assert "ikke tilkoblet" in unconnected.json()["detail"]

        # Unsupported provider returns 422
        bad_provider = client.post(
            "/api/recipe-collections",
            json={"provider": "unknown", "kinds": ["saved"]},
        )
        assert bad_provider.status_code == 422

        # Connect Cookidoo
        _save_cookidoo_connection(store, hid)

        # Oda kind rejected for Cookidoo
        wrong_kind = client.post(
            "/api/recipe-collections",
            json={"provider": "cookidoo", "kinds": ["liked"]},
        )
        assert wrong_kind.status_code == 422

        # Preview Cookidoo saved + created
        preview = client.post(
            "/api/recipe-collections",
            json={"provider": "cookidoo", "kinds": ["saved", "created"]},
        )
        assert preview.status_code == 202, preview.text
        job = _settled(client, preview.json()["job_id"])
        assert job["provider"] == "cookidoo"
        assert job["status"] == "ready"
        assert [e["id"] for e in job["entries"]] == ["r1", "r2"]
        # r1 merged badges from both saved and created
        assert job["entries"][0]["badges"] == ["Lagret i Cookidoo", "Mine oppskrifter"]

        # Import requires reviewed=True
        denied = client.post(
            f"/api/recipe-collections/{job['job_id']}/import",
            json={"selected_ids": ["r1"], "reviewed": False},
        )
        assert denied.status_code == 422

        # Import both r1 (inline) and r2 (fetched from /api/v2/recipes/r2)
        imported = client.post(
            f"/api/recipe-collections/{job['job_id']}/import",
            json={"selected_ids": ["r1", "r2"], "reviewed": True},
        )
        assert imported.status_code == 202, imported.text
        done = _settled(client, job["job_id"])
        assert done["status"] == "completed"
        assert [e["status"] for e in done["entries"]] == ["imported", "imported"]

        with sqlite_engine.connect() as conn:
            saved_rows = conn.execute(select(recipes)).all()
            link_rows = conn.execute(select(source_links)).all()
        assert len(saved_rows) == 2
        assert {row.source for row in saved_rows} == {RecipeSource.COOKIDOO}
        assert {row.source_url for row in saved_rows} == {
            "https://cookidoo.no/recipes/recipe/nb-NO/r1",
            "https://cookidoo.no/recipes/recipe/nb-NO/r2",
        }
        assert {row.provider for row in link_rows} == {"cookidoo"}

        # Default provider still routes to Oda
        oda_preview = client.post("/api/recipe-collections", json={"kinds": ["liked"]})
        assert oda_preview.status_code == 202
        oda_job = _settled(client, oda_preview.json()["job_id"])
        assert oda_job["provider"] == "oda"
        assert oda_job["entries"][0]["id"] == "731"

        # New Cookidoo preview detects already_imported recipes
        second_preview = client.post("/api/recipe-collections", json={"provider": "cookidoo"})
        assert second_preview.status_code == 202
        second_job = _settled(client, second_preview.json()["job_id"])
        assert second_job["kinds"] == ["saved"]
        assert [e["status"] for e in second_job["entries"]] == [
            "already_imported",
            "already_imported",
        ]


@pytest.mark.asyncio
async def test_mcp_cookidoo_collection_tools_are_provider_and_actor_scoped(sqlite_engine, cipher):
    hid = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/user/recipes":
            return httpx.Response(
                200,
                json={
                    "hasMore": False,
                    "recipes": [
                        {
                            "id": "mcp-r1",
                            "title": "Kjøttboller",
                            "servings": 4,
                            "ingredients": ["400 g kjøttdeig", "1 egg"],
                            "instructions": ["Form boller", "Stek i panne"],
                        }
                    ],
                },
            )
        return httpx.Response(404)

    app, features, _, store = _offline_app(
        sqlite_engine,
        cipher,
        household_id=hid,
        cookidoo_transport=httpx.MockTransport(handler),
    )
    _save_cookidoo_connection(store, hid)

    mcp = build_mcp(
        SimpleNamespace(store=SimpleNamespace(engine=sqlite_engine)), lambda: Household()
    )
    install_feature_tools(
        mcp,
        features,
        BasketService(
            SimpleNamespace(store=SimpleNamespace(engine=sqlite_engine)), features.history
        ),
    )

    async with Client(mcp) as _:
        token = history_context.set(HistoryContext(actor_id="alice", channel="mcp"))
        try:
            preview = await mcp.call_tool(
                "preview_recipe_collections",
                {"provider": "cookidoo", "kinds": ["saved"]},
            )
            job_id = preview.structured_content["job_id"]
            assert preview.structured_content["provider"] == "cookidoo"
            await asyncio.sleep(0.05)

            ready = await mcp.call_tool("get_recipe_collection_preview", {"job_id": job_id})
            assert ready.structured_content["status"] == "ready"
            assert ready.structured_content["entries"][0]["id"] == "mcp-r1"

            from fastmcp.exceptions import ToolError

            with pytest.raises(ToolError):
                await mcp.call_tool(
                    "import_recipe_collection_selection",
                    {"job_id": job_id, "selected_ids": ["mcp-r1"], "reviewed": False},
                )

            imported = await mcp.call_tool(
                "import_recipe_collection_selection",
                {"job_id": job_id, "selected_ids": ["mcp-r1"], "reviewed": True},
            )
            assert imported.structured_content["status"] in {"importing", "completed"}
            await asyncio.sleep(0.05)

            done = await mcp.call_tool("get_recipe_collection_preview", {"job_id": job_id})
            assert done.structured_content["status"] == "completed"
            assert done.structured_content["entries"][0]["status"] == "imported"
        finally:
            history_context.reset(token)

        # Different actor cannot read alice's job
        from fastmcp.exceptions import ToolError

        token = history_context.set(HistoryContext(actor_id="bob", channel="mcp"))
        try:
            with pytest.raises(ToolError):
                await mcp.call_tool("get_recipe_collection_preview", {"job_id": job_id})
        finally:
            history_context.reset(token)
