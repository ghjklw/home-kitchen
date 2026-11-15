import json

import httpx
import pytest

from home_kitchen import imports


@pytest.mark.parametrize(
    "url",
    [
        "http://matprat.no/oppskrift",
        "https://matprat.no.evil.example/oppskrift",
        "https://localhost/oppskrift",
        "https://127.0.0.1/oppskrift",
        "https://[::1]/oppskrift",
        "https://user@matprat.no/oppskrift",
        "https://user:pass@matprat.no/oppskrift",
        "https://matprat.no:8443/oppskrift",
        "https://matprat.no:444/oppskrift",
        "https://evil.example/oppskrift",
    ],
)
def test_import_rejects_non_public_or_unsupported_urls(url):
    with pytest.raises(ValueError):
        imports.validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://oda.com/no/recipes/2353-oda-buns/?next=https://evil.example",
        "https://oda.com/no/products/2353-oda-buns/",
        "https://oda.com/no/recipes/not-a-recipe/",
        "https://oda.com/no/recipes/2353-oda-buns/#fragment",
        "https://oda.com:444/no/recipes/2353-oda-buns/",
        "https://user@oda.com/no/recipes/2353-oda-buns/",
    ],
)
def test_oda_import_rejects_urls_outside_exact_public_recipe_shape(url):
    with pytest.raises(ValueError):
        imports.validate_url(url)


def test_matprat_schema_recipe_parses_as_reviewable_draft():
    data = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Tomatsuppe",
        "recipeYield": "4 porsjoner",
        "recipeIngredient": ["1 kg tomater", "2 ss olivenolje"],
        "recipeInstructions": [
            {"@type": "HowToStep", "text": "Del tomatene."},
            {"@type": "HowToStep", "text": "Kok suppen."},
        ],
    }
    html = (
        "<!doctype html><html><head><script type='application/ld+json'>"
        + json.dumps(data)
        + "</script></head><body><h1>Tomatsuppe</h1></body></html>"
    )
    draft = imports.parse_recipe(html, "https://www.matprat.no/oppskrifter/suppe/")
    assert draft.title == "Tomatsuppe"
    assert draft.servings == 4
    assert draft.ingredients == ["1 kg tomater", "2 ss olivenolje"]
    assert draft.instructions == ["Del tomatene.", "Kok suppen."]
    assert str(draft.source_url) == "https://www.matprat.no/oppskrifter/suppe/"


def test_oda_schema_recipe_parses_full_recipe_as_reviewable_draft():
    data = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Buns",
        "recipeYield": "4",
        "image": ["https://images.oda.com/oppskrifter/buns.jpg?fit=bounds&amp;width=2000"],
        "recipeIngredient": ["2 ss Sukker", "1 ts Salt, havsalt", "4.5 dl Vann"],
        "recipeInstructions": [
            {"@type": "HowToStep", "text": "Mix the ingredients."},
            {
                "@type": "HowToSection",
                "itemListElement": [
                    {"@type": "HowToStep", "text": "Let the dough rise."},
                    {"@type": "HowToStep", "text": "Bake until golden."},
                ],
            },
        ],
    }
    html = (
        "<html><head><script type='application/ld+json'>"
        + json.dumps(data)
        + "</script></head></html>"
    )
    draft = imports.parse_recipe(html, "https://oda.com/no/recipes/2353-oda-buns/")
    assert draft.title == "Buns"
    assert draft.servings == 4
    assert draft.ingredients == ["2 ss Sukker", "1 ts Salt, havsalt", "4.5 dl Vann"]
    assert draft.instructions == [
        "Mix the ingredients.",
        "Let the dough rise.",
        "Bake until golden.",
    ]
    assert (
        str(draft.image_url) == "https://images.oda.com/oppskrifter/buns.jpg?fit=bounds&width=2000"
    )
    assert str(draft.source_url) == "https://oda.com/no/recipes/2353-oda-buns/"


@pytest.mark.parametrize(
    "yield_value, expected", [(4, 4), ("4", 4), ("4 porsjoner", 4), ("4 servings", 4)]
)
def test_oda_recipe_accepts_only_unambiguous_positive_servings(yield_value, expected):
    recipe = {
        "@type": "Recipe",
        "name": "Buns",
        "recipeYield": yield_value,
        "recipeIngredient": ["flour"],
        "recipeInstructions": ["Bake."],
    }
    html = "<script type='application/ld+json'>" + json.dumps(recipe) + "</script>"
    draft = imports.parse_recipe(html, "https://oda.com/no/recipes/2353-oda-buns/")
    assert draft.servings == expected


@pytest.mark.parametrize("yield_value", [True, 0, -2, "4-6", "serves four", "about 4", "4 or 6"])
def test_oda_recipe_rejects_ambiguous_or_invalid_servings(yield_value):
    recipe = {
        "@type": "Recipe",
        "name": "Buns",
        "recipeYield": yield_value,
        "recipeIngredient": ["flour"],
        "recipeInstructions": ["Bake."],
    }
    html = "<script type='application/ld+json'>" + json.dumps(recipe) + "</script>"
    with pytest.raises(ValueError, match="complete structured recipe"):
        imports.parse_recipe(html, "https://oda.com/no/recipes/2353-oda-buns/")


@pytest.mark.parametrize("ingredients", [["flour", None], ["flour", "  "], "flour"])
def test_oda_recipe_rejects_malformed_ingredient_entries(ingredients):
    recipe = {
        "@type": "Recipe",
        "name": "Buns",
        "recipeYield": "4",
        "recipeIngredient": ingredients,
        "recipeInstructions": ["Bake."],
    }
    html = "<script type='application/ld+json'>" + json.dumps(recipe) + "</script>"
    with pytest.raises(ValueError, match="complete structured recipe"):
        imports.parse_recipe(html, "https://oda.com/no/recipes/2353-oda-buns/")


def test_oda_recipe_rejects_deeply_nested_instruction_content():
    instruction = {"@type": "HowToStep", "text": "Bake."}
    for _ in range(imports.MAX_JSONLD_DEPTH + 2):
        instruction = {"@type": "HowToSection", "itemListElement": [instruction]}
    recipe = {
        "@type": "Recipe",
        "name": "Buns",
        "recipeYield": "4",
        "recipeIngredient": ["flour"],
        "recipeInstructions": [instruction],
    }
    html = "<script type='application/ld+json'>" + json.dumps(recipe) + "</script>"
    with pytest.raises(ValueError, match="instructions exceed supported limits"):
        imports.parse_recipe(html, "https://oda.com/no/recipes/2353-oda-buns/")


@pytest.mark.parametrize(
    "recipe",
    [
        {
            "@type": "Recipe",
            "name": "Missing yield",
            "recipeIngredient": ["flour"],
            "recipeInstructions": ["Mix"],
        },
        {
            "@type": "Recipe",
            "name": "Missing ingredients",
            "recipeYield": "2",
            "recipeInstructions": ["Mix"],
        },
        {
            "@type": "Recipe",
            "name": "Missing steps",
            "recipeYield": "2",
            "recipeIngredient": ["flour"],
        },
    ],
)
def test_oda_recipe_without_required_content_fails_cleanly(recipe):
    html = "<html><script type='application/ld+json'>" + json.dumps(recipe) + "</script></html>"
    with pytest.raises(ValueError, match="complete structured recipe"):
        imports.parse_recipe(html, "https://oda.com/no/recipes/2353-oda-buns/")


def test_oda_recipe_import_rejects_ambiguous_structured_recipes():
    html = (
        "<script type='application/ld+json'>"
        + json.dumps(
            [
                {"@type": "Recipe", "name": "One"},
                {"@type": "Recipe", "name": "Two"},
            ]
        )
        + "</script>"
    )
    with pytest.raises(ValueError, match="one unambiguous structured recipe"):
        imports.parse_recipe(html, "https://oda.com/no/recipes/2353-oda-buns/")


@pytest.mark.asyncio
async def test_import_fetches_html_without_external_network(monkeypatch):
    client_class = httpx.AsyncClient
    html = """<!doctype html><html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Recipe","name":"Soup",
    "recipeYield":"2 servings","recipeIngredient":["1 onion"],
    "recipeInstructions":[{"@type":"HowToStep","text":"Cook it."}]}
    </script></head><body></body></html>"""

    async def handler(request):
        assert request.url.host == "matprat.no"
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=html)

    monkeypatch.setattr(
        imports.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
    )
    draft = await imports.import_recipe("https://matprat.no/oppskrifter/suppe/")
    assert draft.title == "Soup"
    assert draft.ingredients == ["1 onion"]


@pytest.mark.asyncio
async def test_oda_import_uses_mocked_public_html_and_does_not_follow_to_other_hosts(monkeypatch):
    client_class = httpx.AsyncClient
    html = """<script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Recipe","name":"Buns",
    "recipeYield":"4","recipeIngredient":["flour"],
    "recipeInstructions":[{"@type":"HowToStep","text":"Bake."}]}
    </script>"""
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=html,
        )

    monkeypatch.setattr(
        imports.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
    )
    draft = await imports.import_recipe("https://oda.com/no/recipes/2353-oda-buns/")
    assert draft.title == "Buns"
    assert requests[0].headers["user-agent"] == imports.USER_AGENT

    async def redirect_handler(request):
        return httpx.Response(302, headers={"location": "https://127.0.0.1/metadata"})

    monkeypatch.setattr(
        imports.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(redirect_handler), **kwargs),
    )
    with pytest.raises(ValueError, match="supports public HTTPS"):
        await imports.import_recipe("https://oda.com/no/recipes/2353-oda-buns/")
