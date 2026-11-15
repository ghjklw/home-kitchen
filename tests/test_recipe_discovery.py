from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from home_kitchen.oda.service import OdaError, OdaService
from home_kitchen.recipes.discovery import (
    MATPRAT_INDEX,
    MarmitonDiscoveryProvider,
    MatpratDiscoveryProvider,
    OdaDiscoveryProvider,
    ProviderSearch,
    RecipeDiscoveryItem,
    RecipeDiscoveryRegistry,
    RecipeDiscoveryRequest,
)


def test_query_and_source_request_validation():
    assert RecipeDiscoveryRequest(query="  pasta ").query == "pasta"
    with pytest.raises(ValidationError):
        RecipeDiscoveryRequest(query="  ")
    with pytest.raises(ValidationError):
        RecipeDiscoveryRequest(query="x" * 201)
    with pytest.raises(ValidationError):
        RecipeDiscoveryRequest(query="soup", sources=["marmiton", "marmiton"])


@pytest.mark.asyncio
async def test_marmiton_search_is_bounded_and_uses_only_canonical_recipe_links():
    observed = []
    html = """
    <a href="/recettes/recette_carbonara-traditionnelle_340808.aspx">
      <img src="https://assets.afcdn.com/recipe/340808.jpg" alt="Carbonara traditionnelle">
    </a>
    <a href="/recettes/recette_carbonara-traditionnelle_340808.aspx">Carbonara traditionnelle</a>
    <a href="https://evil.example/recettes/recette_fake_123.aspx">Fake</a>
    <a href="/recettes/recherche.aspx?aqt=carbonara">Search</a>
    <a href="/recettes/recette_other_456.aspx">
      <img src="https://127.0.0.1/pixel.jpg" alt="Other recipe">
    </a>
    """

    def handler(request):
        observed.append(request)
        assert request.url.path == "/recettes/recherche.aspx"
        assert request.url.params.get_list("aqt") == ["carbonara pasta"]
        assert set(request.url.params.keys()) == {"aqt"}
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        response = await MarmitonDiscoveryProvider(http_client=client).search("carbonara pasta", 10)

    assert len(observed) == 1
    assert len(response.results) == 2
    first, second = response.results
    assert (
        str(first.url)
        == "https://www.marmiton.org/recettes/recette_carbonara-traditionnelle_340808.aspx"
    )
    assert first.title == "Carbonara traditionnelle"
    assert str(first.image_url) == "https://assets.afcdn.com/recipe/340808.jpg"
    assert first.importable is True
    assert str(second.url) == "https://www.marmiton.org/recettes/recette_other_456.aspx"
    assert second.image_url is None


@pytest.mark.asyncio
async def test_marmiton_rejects_unsafe_redirect_and_oversized_response():
    redirect_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/"})
        ),
        trust_env=False,
    )
    async with redirect_client:
        with pytest.raises(ValueError, match="redirected"):
            await MarmitonDiscoveryProvider(http_client=redirect_client).search("soup", 5)

    oversized_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html", "content-length": "1000001"},
                content=b"x",
            )
        ),
        trust_env=False,
    )
    async with oversized_client:
        with pytest.raises(ValueError, match="size limit"):
            await MarmitonDiscoveryProvider(http_client=oversized_client).search("soup", 5)


class _StaticProvider:
    def __init__(self, source, response=None, delay=0):
        self.source = source
        self.response = response or ProviderSearch()
        self.timeout_seconds = 0.01 if delay else 1
        self.delay = delay

    async def search(self, query, limit, actor_id=None):
        del query, limit, actor_id
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.response


@pytest.mark.asyncio
async def test_registry_keeps_partial_results_times_out_and_deduplicates_exact_urls():
    duplicate = RecipeDiscoveryItem(
        source="marmiton",
        title="Same recipe",
        url="https://www.marmiton.org/recettes/recette_soup_123.aspx",
        importable=True,
    )
    unique = RecipeDiscoveryItem(
        source="oda",
        title="Other recipe",
        url="https://oda.com/no/recipes/123-other-soup/",
        importable=True,
    )
    registry = RecipeDiscoveryRegistry(
        [
            _StaticProvider("marmiton", ProviderSearch(results=[duplicate])),
            _StaticProvider("oda", ProviderSearch(results=[duplicate, unique])),
            _StaticProvider("matprat", delay=0.1),
        ]
    )

    response = await registry.search(RecipeDiscoveryRequest(query="soup"))

    assert [item.title for item in response.results] == ["Same recipe", "Other recipe"]
    assert [(item.source, item.status) for item in response.sources] == [
        ("marmiton", "ok"),
        ("oda", "ok"),
        ("matprat", "timed_out"),
    ]
    assert [item.result_count for item in response.sources] == [1, 1, 0]


@pytest.mark.asyncio
async def test_oda_search_requires_existing_connection_and_never_guesses_missing_links():
    class Service:
        def __init__(self, connected):
            self.connected = connected
            self.called = False

        def status(self, actor_id):
            assert actor_id == "household-actor"
            return {"connected": self.connected}

        async def search_recipes(self, actor_id, query, page, size):
            self.called = True
            assert (actor_id, query, page, size) == ("household-actor", "soup", 1, 4)
            return {
                "recipes": [
                    {
                        "id": 1,
                        "title": "Public recipe",
                        "url": "https://oda.com/no/recipes/123-good-soup/",
                        "imageUrl": "https://images.oda.com/recipe/photo.jpg",
                        "providerName": "Oda",
                    },
                    {
                        "id": 2,
                        "title": "No provided URL",
                        "imageUrl": "https://evil.example/photo.jpg",
                    },
                    {"id": 3, "title": "Unsafe URL", "url": "https://127.0.0.1/recipe"},
                ]
            }

    service = Service(connected=False)
    provider = OdaDiscoveryProvider(service)
    disconnected = await provider.search("soup", 4, "household-actor")
    assert disconnected.status == "connection_required"
    assert service.called is False

    service.connected = True
    connected = await provider.search("soup", 4, "household-actor")
    assert service.called is True
    assert [item.importable for item in connected.results] == [True, False, False]
    assert connected.results[1].url is None
    assert "did not provide" in connected.results[1].detail
    assert connected.results[2].url is None
    assert connected.results[1].image_url is None
    assert str(connected.results[0].image_url) == "https://images.oda.com/recipe/photo.jpg"


@pytest.mark.asyncio
async def test_matprat_reports_unsupported_search_with_real_index_link():
    result = await MatpratDiscoveryProvider().search("soup", 5)
    assert result.status == "unsupported"
    assert result.results == []
    assert str(result.index_url) == MATPRAT_INDEX


def test_oda_service_verifies_readonly_recipe_tool_schema(monkeypatch):
    tool = SimpleNamespace(
        annotations=SimpleNamespace(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "page": {"type": "integer", "minimum": 1, "maximum": 50},
                "size": {"type": "integer", "minimum": 1, "maximum": 20},
            },
        },
        outputSchema={
            "type": "object",
            "required": ["hasMore", "recipes"],
            "properties": {
                "hasMore": {"type": "boolean"},
                "recipes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "title", "likeCount"],
                        "properties": {
                            "id": {"type": "integer"},
                            "title": {"type": "string"},
                            "likeCount": {"type": "integer"},
                        },
                    },
                },
            },
        },
    )
    expected = {"hasMore": False, "recipes": [{"id": 9, "title": "Soup", "likeCount": 2}]}
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def list_tools(self):
            return [SimpleNamespace(name="recipe_search", **tool.__dict__)]

        async def call_tool(self, name, arguments, raise_on_error):
            calls.append((name, arguments, raise_on_error))
            return SimpleNamespace(structured_content=expected, content=[])

    monkeypatch.setattr("home_kitchen.oda.service.Client", Client)
    service = object.__new__(OdaService)
    service.access_token = lambda actor_id: "dummy-token"
    result = asyncio.run(service.search_recipes("actor", " soup ", size=3))
    assert result["hasMore"] is False
    assert result["recipes"][0]["title"] == "Soup"
    assert result["recipes"][0]["url"] is None
    assert calls == [("recipe_search", {"query": "soup", "page": 1, "size": 3}, True)]

    tool.annotations.readOnlyHint = False
    with pytest.raises(OdaError, match="read-only"):
        asyncio.run(service.search_recipes("actor", "soup"))
