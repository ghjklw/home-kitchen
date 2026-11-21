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
from home_kitchen.cookidoo_plan_sync import CookidooPlanSyncService
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
from home_kitchen.recipes.bootstrap import create_collection_service
from home_kitchen.recipes.collections import (
    CollectionEntry,
    CollectionPage,
    source_links,
)
from home_kitchen.settings import Household, Settings
from home_kitchen.store import recipes

pytestmark = pytest.mark.mocked_provider


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
                    source_language TEXT NOT NULL DEFAULT 'und',
                    recipe_type TEXT,
                    prep_time_minutes INTEGER,
                    prep_category TEXT,
                    guided_steps TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE meals (
                    id TEXT PRIMARY KEY,
                    position INTEGER,
                    date TEXT NOT NULL,
                    label TEXT,
                    scheduled_time TEXT,
                    recipe_id TEXT NOT NULL,
                    servings INTEGER NOT NULL
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
    collection_service = create_collection_service(
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
        cookidoo_plan_sync=CookidooPlanSyncService(
            kitchen,
            history,
            store,
            household_id,
            provider=cookidoo_provider,
        ),
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
    assert entry.source_url == "https://cookidoo.international/recipes/recipe/nb-NO/r12345"
    assert entry.image_url == "https://cookidoo.international/images/r12345.jpg"
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

    # Official Cookidoo recipe detail shape (/recipes/recipe/en/{id})
    official = normalize_cookidoo_recipe(
        {
            "id": "r587526",
            "title": "Gougères au fromage",
            "servingSize": {"quantity": {"value": 20}, "unitNotation": "pièces"},
            "recipeIngredientGroups": [
                {
                    "title": "Pâte",
                    "recipeIngredients": [
                        {
                            "quantity": {"value": 200},
                            "unitNotation": "g",
                            "ingredientNotation": "fromage",
                            "preparation": "râpé",
                        },
                        {
                            "quantity": {"value": 2},
                            "unitNotation": "",
                            "ingredientNotation": "œufs",
                        },
                    ],
                }
            ],
            "recipeStepGroups": [
                {
                    "title": "Préparation",
                    "recipeSteps": [
                        {
                            "title": "1",
                            "formattedText": "Râper le fromage <nobr>10 sec/vitesse 7</nobr>.",
                        }
                    ],
                }
            ],
            "times": [{"type": "totalTime", "quantity": {"value": 1800}}],
            "descriptiveAssets": [
                {
                    "square": "https://assets.tmecosys.com/image/upload/{transformation}/gougeres.jpg",
                }
            ],
        },
        locale="en",
    )
    assert official.id == "r587526"
    assert official.servings == 20
    assert official.ingredients == ["200 g fromage, râpé", "2 œufs"]
    assert official.instructions == ["Râper le fromage 10 sec/vitesse 7."]
    assert official.metadata["total_time"] == 1800
    assert (
        official.image_url
        == "https://assets.tmecosys.com/image/upload/t_web_rdp_recipe_584x480_1_5x/gougeres.jpg"
    )

    # Created recipe item shape (/created-recipes/en)
    created = normalize_cookidoo_recipe(
        {
            "recipeId": "01GCCC2PPVY882SM52F2ZBNQZW",
            "recipeContent": {
                "name": "Compote pomme/prune",
                "image": "https://ugc.assets.tmecosys.com/image/upload/{transformation}/compote.jpg",
                "recipeIngredient": ["500 g pommes", "200 g prunes"],
                "recipeInstructions": ["Cuire <nobr>15 min/100°C</nobr>."],
                "totalTime": 900,
                "recipeYield": "6 portions",
            },
        },
        locale="en",
    )
    assert created.id == "01GCCC2PPVY882SM52F2ZBNQZW"
    assert created.title == "Compote pomme/prune"
    assert created.servings == 6
    assert (
        created.image_url
        == "https://ugc.assets.tmecosys.com/image/upload/t_web_rdp_recipe_584x480_1_5x/compote.jpg"
    )
    assert created.ingredients == ["500 g pommes", "200 g prunes"]
    assert created.instructions == ["Cuire 15 min/100°C."]
    assert created.metadata["total_time"] == 900


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
        "https://cookidoo.international/recipes/recipe/nb-NO/r100",
        "https://cookidoo.international/created-recipes/nb-NO/c1",
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

        if request.url.path == "/organize/en/api/custom-list":
            return httpx.Response(
                200,
                json={
                    "customlists": [
                        {
                            "id": "list-1",
                            "title": "Favoritter",
                            "recipeIds": ["r10", "r-missing"],
                            "listThumbnail": "https://assets.tmecosys.com/image/upload/{transformation}/thumb.jpg",
                        }
                    ],
                },
            )
        if request.url.path == "/recipes/recipe/en/r10":
            return httpx.Response(
                200,
                json={
                    "id": "r10",
                    "title": "Fiskegrateng",
                    "servingSize": {"quantity": {"value": 4}},
                    "recipeIngredientGroups": [
                        {
                            "recipeIngredients": [
                                {
                                    "quantity": {"value": 400},
                                    "unitNotation": "g",
                                    "ingredientNotation": "torsk",
                                },
                                {
                                    "quantity": {"value": 2},
                                    "unitNotation": "dl",
                                    "ingredientNotation": "makaroni",
                                },
                            ]
                        }
                    ],
                    "recipeStepGroups": [
                        {
                            "recipeSteps": [
                                {"formattedText": "Kok makaroni"},
                                {"formattedText": "Stek i ovn <nobr>30 min</nobr>"},
                            ]
                        }
                    ],
                },
            )
        if request.url.path == "/recipes/recipe/en/r-missing":
            return httpx.Response(404)
        if request.url.path == "/created-recipes/en":
            return httpx.Response(
                200,
                json={
                    "meta": {"totalCount": 1},
                    "items": [
                        {
                            "recipeId": "c1",
                            "recipeContent": {
                                "name": "Egen vaffelrøre",
                                "image": "https://ugc.assets.tmecosys.com/image/upload/{transformation}/vaffel.jpg",
                                "recipeIngredient": ["3 dl hvetemel", "2 egg"],
                                "recipeInstructions": ["Visp sammen", "Stek vafler"],
                                "recipeYield": 4,
                            },
                        }
                    ],
                },
            )
        if request.url.path == "/organize/en/api/managed-list":
            return httpx.Response(401)
        return httpx.Response(500)

    provider = CookidooCollectionProvider(
        connector_store, hid, transport=httpx.MockTransport(handler)
    )

    saved_page = await provider.page("malo", "saved", 1, 20)
    assert saved_page.has_more is False
    assert [e.id for e in saved_page.entries] == ["r10", "r-missing"]
    assert saved_page.entries[0].title == "Fiskegrateng"
    assert saved_page.entries[0].badges == ["Favoritter"]
    assert saved_page.entries[0].ingredients == ["400 g torsk", "2 dl makaroni"]
    assert saved_page.entries[0].instructions == ["Kok makaroni", "Stek i ovn 30 min"]
    # Graceful fallback when single recipe detail 404s
    assert saved_page.entries[1].title == "Favoritter (r-missing)"
    assert (
        saved_page.entries[1].image_url
        == "https://assets.tmecosys.com/image/upload/t_web_rdp_recipe_584x480_1_5x/thumb.jpg"
    )

    created_page = await provider.page("malo", "created", 1, 20)
    assert created_page.has_more is False
    assert created_page.entries[0].id == "c1"
    assert created_page.entries[0].title == "Egen vaffelrøre"
    assert (
        created_page.entries[0].source_url == "https://cookidoo.international/created-recipes/en/c1"
    )
    assert (
        created_page.entries[0].image_url
        == "https://ugc.assets.tmecosys.com/image/upload/t_web_rdp_recipe_584x480_1_5x/vaffel.jpg"
    )
    assert created_page.entries[0].ingredients == ["3 dl hvetemel", "2 egg"]

    with pytest.raises(CookidooError) as auth_err:
        await provider.page("malo", "bookmarks", 1, 20)
    assert auth_err.value.status_code == 401
    assert "logg inn" in auth_err.value.detail.lower()

    with pytest.raises(CookidooError) as kind_err:
        await provider.page("malo", "unknown", 1, 20)
    assert kind_err.value.status_code == 400


@pytest.mark.asyncio
async def test_cookidoo_provider_html_response_returns_clean_502_and_bookmarks_managed_list(
    connector_store,
):
    hid = uuid4()
    _save_cookidoo_connection(connector_store, hid, access_token="tok-abc")

    def html_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<!doctype html><html><body>Login</body></html>")

    html_provider = CookidooCollectionProvider(
        connector_store, hid, transport=httpx.MockTransport(html_handler)
    )
    with pytest.raises(CookidooError) as exc:
        await html_provider.page("malo", "created", 1, 20)
    assert exc.value.status_code == 502
    assert "JSON" in exc.value.detail

    def bookmarks_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/organize/en/api/managed-list":
            return httpx.Response(
                200,
                json={
                    "managedlists": [
                        {
                            "collectionId": "col338667",
                            "title": "Min Kokebok",
                            "recipeCount": 2,
                            "landscapeImage": "https://assets.tmecosys.com/image/upload/{transformation}/col.jpg",
                        }
                    ]
                },
            )
        if request.url.path == "/organize/en/api/managed-list/col338667":
            return httpx.Response(
                200,
                json={
                    "title": "Min Kokebok",
                    "chapters": [
                        {
                            "title": "Hovedretter",
                            "recipes": [
                                {
                                    "id": "r201",
                                    "title": "Lasagne",
                                    "descriptiveAssets": [
                                        {
                                            "square": "https://assets.tmecosys.com/image/upload/{transformation}/lasagne.jpg"
                                        }
                                    ],
                                }
                            ],
                            "recipeIds": ["r202"],
                        }
                    ],
                },
            )
        if request.url.path == "/recipes/recipe/en/r202":
            return httpx.Response(
                200,
                json={
                    "id": "r202",
                    "title": "Kyllingcurry",
                    "servingSize": {"quantity": {"value": 4}},
                    "recipeIngredientGroups": [{"recipeIngredients": ["500 g kylling"]}],
                    "recipeStepGroups": [{"recipeSteps": [{"formattedText": "Stek kylling"}]}],
                },
            )
        return httpx.Response(404)

    bm_provider = CookidooCollectionProvider(
        connector_store, hid, transport=httpx.MockTransport(bookmarks_handler)
    )
    bm_page = await bm_provider.page("malo", "bookmarks", 1, 20)
    assert bm_page.has_more is False
    assert [e.id for e in bm_page.entries] == ["r201", "r202"]
    assert bm_page.entries[0].title == "Lasagne"
    assert bm_page.entries[0].badges == ["Min Kokebok"]
    assert bm_page.entries[1].title == "Kyllingcurry"
    assert bm_page.entries[1].badges == ["Min Kokebok"]


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
        if request.url.path == "/organize/en/api/custom-list":
            return httpx.Response(
                200,
                json={
                    "customlists": [
                        {
                            "id": "cl-1",
                            "title": "Lagret i Cookidoo",
                            "recipeIds": ["r1", "r2"],
                        }
                    ]
                },
            )
        if request.url.path == "/recipes/recipe/en/r1":
            return httpx.Response(
                200,
                json={
                    "id": "r1",
                    "title": "Risotto med sopp",
                    "servingSize": {"quantity": {"value": 4}},
                    "recipeIngredientGroups": [
                        {
                            "recipeIngredients": [
                                {
                                    "quantity": {"value": 300},
                                    "unitNotation": "g",
                                    "ingredientNotation": "risottoris",
                                },
                                {
                                    "quantity": {"value": 200},
                                    "unitNotation": "g",
                                    "ingredientNotation": "sjampinjong",
                                },
                            ]
                        }
                    ],
                    "recipeStepGroups": [
                        {
                            "recipeSteps": [
                                {"formattedText": "Stek sopp"},
                                {"formattedText": "Tilsett kraft litt etter litt"},
                            ]
                        }
                    ],
                },
            )
        if request.url.path == "/recipes/recipe/en/r2":
            return httpx.Response(
                200,
                json={
                    "id": "r2",
                    "title": "Pannekaker",
                    "servingSize": {"quantity": {"value": 4}},
                    "recipeIngredientGroups": [
                        {
                            "recipeIngredients": [
                                "3 dl hvetemel",
                                "6 dl melk",
                                "3 egg",
                            ]
                        }
                    ],
                    "recipeStepGroups": [
                        {
                            "recipeSteps": [
                                {"formattedText": "Visp sammen røren"},
                                {"formattedText": "Stek gylne pannekaker"},
                            ]
                        }
                    ],
                },
            )
        if request.url.path == "/created-recipes/en":
            return httpx.Response(
                200,
                json={
                    "meta": {"totalCount": 2},
                    "items": [
                        {
                            "recipeId": "r1",
                            "badges": ["Mine oppskrifter"],
                            "recipeContent": {
                                "name": "Risotto med sopp",
                                "recipeIngredient": ["300 g risottoris", "200 g sjampinjong"],
                                "recipeInstructions": [
                                    "Stek sopp",
                                    "Tilsett kraft litt etter litt",
                                ],
                                "recipeYield": 4,
                            },
                        },
                        {
                            "recipeId": "01GCCC2PPVY882SM52F2ZBNQZW",
                            "recipeContent": {
                                "name": "Compote pomme/prune",
                                "image": "https://ugc.assets.tmecosys.com/image/upload/{transformation}/compote.jpg",
                                "recipeIngredient": ["500 g pommes", "200 g prunes"],
                                "recipeInstructions": ["Cuire <nobr>15 min</nobr>."],
                                "recipeYield": 4,
                            },
                        },
                    ],
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
        assert [e["id"] for e in job["entries"]] == ["r1", "r2", "01GCCC2PPVY882SM52F2ZBNQZW"]
        # r1 merged badges from both saved and created
        assert job["entries"][0]["badges"] == ["Lagret i Cookidoo", "Mine oppskrifter"]
        assert (
            job["entries"][2]["image_url"]
            == "https://ugc.assets.tmecosys.com/image/upload/t_web_rdp_recipe_584x480_1_5x/compote.jpg"
        )

        # Import requires reviewed=True
        denied = client.post(
            f"/api/recipe-collections/{job['job_id']}/import",
            json={"selected_ids": ["r1"], "reviewed": False},
        )
        assert denied.status_code == 422

        # Import r1, r2, and created recipe (without hitting 404)
        imported = client.post(
            f"/api/recipe-collections/{job['job_id']}/import",
            json={"selected_ids": ["r1", "r2", "01GCCC2PPVY882SM52F2ZBNQZW"], "reviewed": True},
        )
        assert imported.status_code == 202, imported.text
        done = _settled(client, job["job_id"])
        assert done["status"] == "completed"
        assert [e["status"] for e in done["entries"]] == ["imported", "imported", "imported"]

        with sqlite_engine.connect() as conn:
            saved_rows = conn.execute(select(recipes)).all()
            link_rows = conn.execute(select(source_links)).all()
        assert len(saved_rows) == 3
        assert {row.source for row in saved_rows} == {RecipeSource.COOKIDOO}
        assert {row.source_url for row in saved_rows} == {
            "https://cookidoo.international/recipes/recipe/en/r1",
            "https://cookidoo.international/recipes/recipe/en/r2",
            "https://cookidoo.international/created-recipes/en/01GCCC2PPVY882SM52F2ZBNQZW",
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
        if request.url.path == "/organize/en/api/custom-list":
            return httpx.Response(
                200,
                json={
                    "customlists": [
                        {
                            "id": "cl-mcp",
                            "title": "Lagret i Cookidoo",
                            "recipeIds": ["mcp-r1"],
                        }
                    ]
                },
            )
        if request.url.path == "/recipes/recipe/en/mcp-r1":
            return httpx.Response(
                200,
                json={
                    "id": "mcp-r1",
                    "title": "Kjøttboller",
                    "servingSize": {"quantity": {"value": 4}},
                    "recipeIngredientGroups": [{"recipeIngredients": ["400 g kjøttdeig", "1 egg"]}],
                    "recipeStepGroups": [
                        {
                            "recipeSteps": [
                                {"formattedText": "Form boller"},
                                {"formattedText": "Stek i panne"},
                            ]
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


@pytest.mark.asyncio
async def test_cookidoo_domain_resolution_uses_international_for_norway(connector_store):
    from home_kitchen.cookidoo_recipes import (
        DEFAULT_COOKIDOO_NO_URL,
        _cookidoo_domain_for_country,
    )

    assert DEFAULT_COOKIDOO_NO_URL == "https://cookidoo.international"
    for country in (None, "", "no", "NO", "nb", "nn", "nb-no", "international"):
        assert _cookidoo_domain_for_country(country) == "cookidoo.international"
    assert _cookidoo_domain_for_country("de") == "cookidoo.de"

    hid = uuid4()
    _save_cookidoo_connection(connector_store, hid, country="no")
    hosts_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts_seen.append(request.url.host)
        if request.url.path == "/organize/en/api/custom-list":
            return httpx.Response(
                200,
                json={"customlists": [{"id": "cl1", "title": "Favoritter", "recipeIds": ["r42"]}]},
            )
        if request.url.path == "/recipes/recipe/en/r42":
            return httpx.Response(
                200,
                json={
                    "id": "r42",
                    "title": "Fiskesuppe",
                    "ingredients": ["400 g laks"],
                    "instructions": ["Kok opp kraft"],
                },
            )
        return httpx.Response(404)

    provider = CookidooCollectionProvider(
        connector_store, hid, transport=httpx.MockTransport(handler)
    )
    page = await provider.page("malo", "saved", 1, 20)
    assert hosts_seen == ["cookidoo.international", "cookidoo.international"]
    assert page.entries[0].source_url == "https://cookidoo.international/recipes/recipe/en/r42"

    draft = await provider.extract_entry(page.entries[0])
    assert str(draft.source_url) == "https://cookidoo.international/recipes/recipe/en/r42"


@pytest.mark.asyncio
async def test_cookidoo_custom_list_supports_chapters_and_hydrates_missing_images(connector_store):
    hid = uuid4()
    _save_cookidoo_connection(connector_store, hid, country="no")
    paths_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths_seen.append(request.url.path)
        if request.url.path == "/organize/en/api/custom-list":
            return httpx.Response(
                200,
                json={
                    "customlists": [
                        {
                            "id": "cl-chapters",
                            "title": "Ukemeny",
                            "chapters": [
                                {
                                    "title": "Forretter",
                                    "recipes": [
                                        # Inline summary without image_url / descriptiveAssets -> MUST hydrate
                                        {"id": "r-no-img", "title": "Tomatsuppe"},
                                    ],
                                    "recipeIds": ["r-chap-id"],
                                }
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/recipes/recipe/en/r-no-img":
            return httpx.Response(
                200,
                json={
                    "id": "r-no-img",
                    "title": "Tomatsuppe",
                    "servingSize": {"quantity": {"value": 4}},
                    "times": [{"type": "totalTime", "quantity": {"value": 1500}}],
                    "descriptiveAssets": [
                        {
                            "square": "https://assets.tmecosys.com/image/upload/{transformation}/tomatsuppe.jpg"
                        }
                    ],
                    "recipeIngredientGroups": [{"recipeIngredients": ["500 g tomater"]}],
                    "recipeStepGroups": [{"recipeSteps": [{"formattedText": "Kok opp"}]}],
                },
            )
        if request.url.path == "/recipes/recipe/en/r-chap-id":
            return httpx.Response(
                200,
                json={
                    "id": "r-chap-id",
                    "title": "Sjokolademousse",
                    "servingSize": {"quantity": {"value": 4}},
                    "times": [{"type": "totalTime", "quantity": {"value": 900}}],
                    "categories": [{"title": "Desserts"}],
                    "descriptiveAssets": [
                        {
                            "landscape": "https://assets.tmecosys.com/image/upload/{transformation}/mousse.jpg"
                        }
                    ],
                    "recipeIngredientGroups": [{"recipeIngredients": ["200 g sjokolade"]}],
                    "recipeStepGroups": [{"recipeSteps": [{"formattedText": "Pisk sammen"}]}],
                },
            )
        return httpx.Response(404)

    provider = CookidooCollectionProvider(
        connector_store, hid, transport=httpx.MockTransport(handler)
    )
    page = await provider.page("malo", "saved", 1, 20)
    assert [e.id for e in page.entries] == ["r-no-img", "r-chap-id"]
    assert "/recipes/recipe/en/r-no-img" in paths_seen
    assert (
        page.entries[0].image_url
        == "https://assets.tmecosys.com/image/upload/t_web_rdp_recipe_584x480_1_5x/tomatsuppe.jpg"
    )
    assert page.entries[0].recipe_type == "lunsj"
    assert page.entries[0].prep_time_minutes == 25
    assert page.entries[0].prep_category == "rask"

    assert (
        page.entries[1].image_url
        == "https://assets.tmecosys.com/image/upload/t_web_rdp_recipe_584x480_1_5x/mousse.jpg"
    )
    assert page.entries[1].recipe_type == "dessert"
    assert page.entries[1].prep_time_minutes == 15
    assert page.entries[1].prep_category == "rask"

    # extract_entry preserves image_url and auto-tags on ImportedDraft
    draft = await provider.extract_entry(page.entries[0])
    assert (
        str(draft.image_url)
        == "https://assets.tmecosys.com/image/upload/t_web_rdp_recipe_584x480_1_5x/tomatsuppe.jpg"
    )
    assert draft.recipe_type == "lunsj"
    assert draft.prep_time_minutes == 25
    assert draft.prep_category == "rask"


def test_cookidoo_auto_tags_and_import_atomic_persists_metadata(connector_store, sqlite_engine):
    hid = uuid4()
    _save_cookidoo_connection(connector_store, hid)
    kitchen = SimpleNamespace(store=SimpleNamespace(engine=sqlite_engine))
    history = FakeHistory()
    service = CookidooRecipeService(kitchen, history, connector_store, hid)

    entry = normalize_cookidoo_recipe(
        {
            "id": "r-tags-1",
            "title": "Brioche maison",
            "servingSize": {"quantity": {"value": 8}},
            "times": [{"type": "totalTime", "quantity": {"value": 5400}}],
            "categories": [{"title": "Baking"}],
            "tags": ["brød", "helg"],
            "descriptiveAssets": [
                {"square": "https://assets.tmecosys.com/image/upload/{transformation}/brioche.jpg"}
            ],
            "recipeIngredientGroups": [{"recipeIngredients": ["500 g hvetemel"]}],
            "recipeStepGroups": [{"recipeSteps": [{"formattedText": "Elt og stek"}]}],
        },
        country="no",
    )
    assert entry.recipe_type == "bakst"
    assert entry.prep_time_minutes == 90
    assert entry.prep_category == "batch_cooking"

    recipe_id, existed = service.import_recipe_atomic("malo", entry, reviewed=True)
    assert existed is False
    with sqlite_engine.connect() as conn:
        row = conn.execute(select(recipes).where(recipes.c.id == recipe_id)).one()
        assert (
            row.image_url
            == "https://assets.tmecosys.com/image/upload/t_web_rdp_recipe_584x480_1_5x/brioche.jpg"
        )
        assert row.recipe_type == "bakst"
        assert row.prep_time_minutes == 90
        assert row.prep_category == "batch_cooking"


def test_normalize_cookidoo_recipe_preserves_tm6_guided_steps_and_ingredient_refs():
    entry = normalize_cookidoo_recipe(
        {
            "id": "r-tm6-1",
            "title": "Risotto i Thermomix",
            "servingSize": {"quantity": {"value": 4}},
            "recipeIngredientGroups": [
                {
                    "recipeIngredients": [
                        {"quantity": {"value": 1}, "unitNotation": "", "ingredientNotation": "løk"},
                        {
                            "quantity": {"value": 300},
                            "unitNotation": "g",
                            "ingredientNotation": "risottoris",
                        },
                        {
                            "quantity": {"value": 800},
                            "unitNotation": "g",
                            "ingredientNotation": "kraft",
                        },
                    ]
                }
            ],
            "recipeStepGroups": [
                {
                    "recipeSteps": [
                        {
                            "formattedText": "Hakk løk <nobr>5 sek/hastighet 5</nobr>.",
                            "ingredientReferences": ["løk"],
                        },
                        {
                            "formattedText": "Tilsett ris og kraft, kok <nobr>15 min/100°C/revers/hastighet 1</nobr>.",
                            "thermomixParameters": {
                                "timeSeconds": 900,
                                "temperature": 100,
                                "speed": "1",
                                "reverse": True,
                                "accessory": "Simmering basket",
                                "mode": "Guided",
                            },
                            "annotations": [
                                {"type": "INGREDIENT", "data": {"index": 1}},
                                {"type": "INGREDIENT", "data": {"ingredient": {"name": "kraft"}}},
                            ],
                        },
                        {
                            "formattedText": "Pisk inn smør med sommerfugl <nobr>2 min/Varoma/Spoon</nobr>.",
                            "blade": "Butterfly whisk",
                        },
                    ]
                }
            ],
        },
        country="no",
    )
    assert len(entry.instructions) == 3
    assert entry.instructions[0] == "Hakk løk 5 sek/hastighet 5."
    assert len(entry.guided_steps) == 3

    step0 = entry.guided_steps[0]
    assert step0.step_index == 0
    assert step0.time_seconds == 5
    assert step0.speed == "5"
    assert step0.ingredient_references == ["løk"]
    assert step0.ingredient_indices == [0]

    step1 = entry.guided_steps[1]
    assert step1.step_index == 1
    assert step1.time_seconds == 900
    assert step1.temperature == "100°C"
    assert step1.speed == "1"
    assert step1.direction == "reverse"
    assert step1.reverse is True
    assert step1.accessory == "Simmering basket"
    assert step1.mode == "Guided"
    assert step1.ingredient_indices == [1, 2]
    assert "300 g risottoris" in step1.ingredient_references
    assert "kraft" in step1.ingredient_references

    step2 = entry.guided_steps[2]
    assert step2.step_index == 2
    assert step2.time_seconds == 120
    assert step2.temperature == "Varoma"
    assert step2.speed == "Spoon"
    assert step2.blade == "Butterfly whisk"


def test_cookidoo_import_persists_guided_steps_and_ai_adaptation_stays_independent(
    connector_store, sqlite_engine
):
    hid = uuid4()
    _save_cookidoo_connection(connector_store, hid)
    kitchen = SimpleNamespace(store=SimpleNamespace(engine=sqlite_engine))
    history = FakeHistory()
    service = CookidooRecipeService(kitchen, history, connector_store, hid)

    entry = normalize_cookidoo_recipe(
        {
            "id": "r-tm6-save",
            "title": "TM6 Suppe",
            "ingredients": ["500 g tomat", "1 løk"],
            "steps": [
                {
                    "formattedText": "Kok opp <nobr>10 min/100°C/hastighet 2</nobr>",
                    "timeSeconds": 600,
                    "temperature": "100°C",
                    "speed": "2",
                    "reverse": True,
                    "blade": "Kniv",
                    "ingredientIndices": [0, 1],
                }
            ],
        },
        country="no",
    )
    recipe_id, existed = service.import_recipe_atomic("malo", entry, reviewed=True)
    assert existed is False
    with sqlite_engine.connect() as conn:
        row = conn.execute(select(recipes).where(recipes.c.id == recipe_id)).mappings().one()
        assert row["source"] == RecipeSource.COOKIDOO
        assert row["source_url"] == "https://cookidoo.international/recipes/recipe/nb-NO/r-tm6-save"
        assert row["instructions"] == ["Kok opp 10 min/100°C/hastighet 2"]
        import json as _json

        stored_steps = (
            _json.loads(row["guided_steps"])
            if isinstance(row["guided_steps"], str)
            else row["guided_steps"]
        )
        assert len(stored_steps) == 1
        assert stored_steps[0]["time_seconds"] == 600
        assert stored_steps[0]["temperature"] == "100°C"
        assert stored_steps[0]["speed"] == "2"
        assert stored_steps[0]["direction"] == "reverse"
        assert stored_steps[0]["reverse"] is True
        assert stored_steps[0]["blade"] == "Kniv"
        assert stored_steps[0]["ingredient_indices"] == [0, 1]


@pytest.mark.asyncio
async def test_cookidoo_ai_adaptation_creates_independent_draft_keeping_native_steps(
    connector_store, sqlite_engine
):
    from pydantic_ai.models.test import TestModel

    from home_kitchen.ai import recipe_agent
    from home_kitchen.models import Recipe
    from home_kitchen.recipes import RecipeContext, RecipeDraftRequest, create_recipe_providers

    hid = uuid4()
    _save_cookidoo_connection(connector_store, hid)
    kitchen = SimpleNamespace(store=SimpleNamespace(engine=sqlite_engine))
    history = FakeHistory()
    service = CookidooRecipeService(kitchen, history, connector_store, hid)

    entry = normalize_cookidoo_recipe(
        {
            "id": "r-native-1",
            "title": "Native TM6 Gryte",
            "ingredients": ["400 g kylling", "2 dl fløte"],
            "steps": [
                {
                    "formattedText": "Kok <nobr>12 min/100°C/revers/hastighet 1</nobr>",
                    "timeSeconds": 720,
                    "temperature": "100°C",
                    "speed": "1",
                    "reverse": True,
                }
            ],
        },
        country="no",
    )
    recipe_id, _ = service.import_recipe_atomic("malo", entry, reviewed=True)
    with sqlite_engine.connect() as conn:
        row = dict(conn.execute(select(recipes).where(recipes.c.id == recipe_id)).mappings().one())
        import json as _json

        for col in ("ingredients", "instructions", "source_ingredients", "guided_steps"):
            if isinstance(row[col], str):
                row[col] = _json.loads(row[col])
    native_recipe = Recipe.model_validate(row)
    assert len(native_recipe.guided_steps) == 1

    settings = Settings(local_dev=True)
    model = TestModel(
        custom_output_args={
            "title": "Vegetarisk gryte (KI-tilpasset)",
            "servings": 4,
            "ingredients": [
                {"name": "kikert", "quantity": "400", "unit": "g", "source_indices": [0]},
                {"name": "havrefløte", "quantity": "2", "unit": "dl", "source_indices": [1]},
            ],
            "instructions": ["Kok opp uten Thermomix på komfyr i 12 min."],
            "source": "ai",
            "source_url": str(native_recipe.source_url),
            "source_ingredients": [
                {"index": 0, "text": "400 g kylling"},
                {"index": 1, "text": "2 dl fløte"},
            ],
            "provenance": {
                "kind": "ai_adaptation",
                "original_source": "cookidoo",
                "source_url": str(native_recipe.source_url),
            },
        }
    )
    providers = create_recipe_providers(settings, recipe_agent(settings, model))
    draft = await providers.draft(
        "ai",
        RecipeDraftRequest(recipe=native_recipe, prompt="Gjør den vegetarisk"),
        RecipeContext(household=Household()),
    )
    assert draft.adapted is not None
    assert draft.adapted.source == "ai"
    assert draft.adapted.provenance is not None
    assert draft.adapted.provenance.original_source == RecipeSource.COOKIDOO
    assert str(draft.adapted.provenance.source_url) == str(native_recipe.source_url)
    # Original Cookidoo recipe in DB remains untouched with native TM6 steps & source_url
    with sqlite_engine.connect() as conn:
        reloaded = conn.execute(select(recipes).where(recipes.c.id == recipe_id)).mappings().one()
        assert reloaded["source"] == RecipeSource.COOKIDOO
        assert reloaded["title"] == "Native TM6 Gryte"
        assert reloaded["guided_steps"][0]["temperature"] == "100°C"


def test_alembic_migration_0029_guided_steps_structure_and_single_head():
    import runpy
    from pathlib import Path
    from unittest.mock import patch

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    migration_path = (
        Path(__file__).parents[1] / "migrations" / "versions" / "0029_recipe_guided_steps.py"
    )
    assert migration_path.exists()
    migration = runpy.run_path(str(migration_path))
    assert migration["revision"] == "0029_recipe_guided_steps"
    assert len(migration["revision"]) <= 32
    assert migration["down_revision"] == "0028_identity_correction_audit"

    with (
        patch.object(migration["op"], "add_column") as mock_add,
        patch.object(migration["op"], "drop_column") as mock_drop,
    ):
        migration["upgrade"]()
        mock_add.assert_called_once()
        assert mock_add.call_args[0][0] == "recipes"
        assert mock_add.call_args[0][1].name == "guided_steps"

        migration["downgrade"]()
        mock_drop.assert_called_once_with("recipes", "guided_steps")

    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    revisions = {rev.revision for rev in script.walk_revisions()}
    assert "0029_recipe_guided_steps" in revisions


@pytest.mark.asyncio
async def test_ai_adaptation_never_invents_or_alters_tm6_device_instructions():
    from pydantic_ai.models.test import TestModel

    from home_kitchen.ai import recipe_agent
    from home_kitchen.models import GuidedCookingStep, RecipeCreate
    from home_kitchen.recipes import RecipeContext, RecipeDraftRequest, create_recipe_providers

    settings = Settings(local_dev=True)

    # 1. Source without guided_steps: even if AI fabricates TM6 steps, application strips them
    manual_source = RecipeCreate(
        title="Vanlig suppe",
        servings=4,
        ingredients=[{"name": "tomat", "quantity": "500", "unit": "g"}],
        instructions=["Kok opp tomater i kjele."],
        source=RecipeSource.MANUAL,
    )
    fabricating_model = TestModel(
        custom_output_args={
            "title": "Vanlig suppe (KI-utkast)",
            "servings": 4,
            "ingredients": [
                {"name": "tomat", "quantity": "500", "unit": "g", "source_indices": [0]}
            ],
            "instructions": ["Kok opp i kjele i 10 min."],
            "source": "ai",
            "source_ingredients": [{"index": 0, "text": "500 g tomat"}],
            "guided_steps": [
                {
                    "step_index": 0,
                    "text": "Kok opp i 10 min.",
                    "time_seconds": 600,
                    "temperature": "100°C",
                    "speed": "3",
                }
            ],
        }
    )
    providers = create_recipe_providers(settings, recipe_agent(settings, fabricating_model))
    draft_no_source_steps = await providers.draft(
        "ai",
        RecipeDraftRequest(recipe=manual_source, prompt="Gjør den raskere"),
        RecipeContext(household=Household()),
    )
    assert draft_no_source_steps.adapted is not None
    assert draft_no_source_steps.adapted.guided_steps == []

    # 2. Source WITH TM6 guided_steps: fabricated/altered settings are dropped, verbatim preserved settings kept
    cookidoo_source = RecipeCreate(
        title="TM6 Suppe",
        servings=4,
        ingredients=[{"name": "tomat", "quantity": "500", "unit": "g"}],
        instructions=["Kok opp 10 min/100°C/hastighet 2"],
        source=RecipeSource.COOKIDOO,
        source_url="https://cookidoo.international/recipes/recipe/nb-NO/r-tm6-guard",
        guided_steps=[
            GuidedCookingStep(
                step_index=0,
                text="Kok opp 10 min/100°C/hastighet 2",
                time_seconds=600,
                temperature="100°C",
                speed="2",
            )
        ],
    )
    mixed_model = TestModel(
        custom_output_args={
            "title": "TM6 Suppe (KI-utkast)",
            "servings": 4,
            "ingredients": [
                {"name": "tomat", "quantity": "500", "unit": "g", "source_indices": [0]}
            ],
            "instructions": [
                "Kok opp 10 min/100°C/hastighet 2",
                "Kjør videre på høy varme.",
            ],
            "source": "ai",
            "source_url": str(cookidoo_source.source_url),
            "source_ingredients": [{"index": 0, "text": "500 g tomat"}],
            "provenance": {
                "kind": "ai_adaptation",
                "original_source": "cookidoo",
                "source_url": str(cookidoo_source.source_url),
            },
            "guided_steps": [
                {
                    "step_index": 0,
                    "text": "Kok opp 10 min/100°C/hastighet 2",
                    "time_seconds": 600,
                    "temperature": "100°C",
                    "speed": "2",
                },
                {
                    "step_index": 1,
                    "text": "Kjør videre på høy varme.",
                    "time_seconds": 300,
                    "temperature": "120°C",
                    "speed": "10",
                },
            ],
        }
    )
    providers_mixed = create_recipe_providers(settings, recipe_agent(settings, mixed_model))
    draft_with_steps = await providers_mixed.draft(
        "ai",
        RecipeDraftRequest(recipe=cookidoo_source, prompt="Tilpass krydder"),
        RecipeContext(household=Household()),
    )
    assert draft_with_steps.adapted is not None
    assert len(draft_with_steps.adapted.guided_steps) == 1
    assert draft_with_steps.adapted.guided_steps[0].temperature == "100°C"
    assert draft_with_steps.adapted.guided_steps[0].speed == "2"
