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
