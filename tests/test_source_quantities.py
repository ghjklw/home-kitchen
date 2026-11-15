from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.models.test import TestModel

from home_kitchen.ai import recipe_agent
from home_kitchen.imports import ImportedDraft
from home_kitchen.recipes.models import EditableRecipeDraft, RecipeContext, RecipeDraftRequest
from home_kitchen.recipes.providers import create_recipe_providers
from home_kitchen.recipes.source_quantities import explicit_source_measure
from home_kitchen.settings import Household, Settings


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1klypeSalt", (Decimal("1"), "klype")),
        ("1 håndfull Kruspersille", (Decimal("1"), "håndfull")),
        ("1,5 ts pepper", (Decimal("1.5"), "ts")),
        ("2 ss olje", (Decimal("2"), "ss")),
        ("3 stk egg", (Decimal("3"), "stk")),
        ("150 g fisk", (Decimal("150"), "g")),
        ("0.5dl fløte", (Decimal("0.5"), "dl")),
        ("salt etter smak", None),
        ("en klype salt", None),
        ("1-2 ts salt", None),
        ("1/2 ts salt", None),
        ("1 laks", None),
        ("1 pakke pasta", None),
        ("0 g salt", None),
    ],
)
def test_explicit_source_measure_is_conservative(text, expected):
    assert explicit_source_measure(text) == expected


def provider_for(rows):
    settings = Settings(local_dev=True)
    return create_recipe_providers(
        settings,
        recipe_agent(
            settings,
            TestModel(
                custom_output_args={
                    "title": "Laks med kremet sitronpasta",
                    "servings": 99,
                    "ingredients": rows,
                    "instructions": ["Cook"],
                    "source_ingredients": [{"index": i, "text": "model wording"} for i in range(5)],
                }
            ),
        ),
    )


def source_recipe():
    return ImportedDraft(
        title="Laks med kremet sitronpasta",
        servings=4,
        ingredients=[
            "1klypeSalt",
            "1klypeSalt",
            "1klypePepper",
            "1klypePepper",
            "1håndfullKruspersille",
        ],
        instructions=["Cook"],
        source_url="https://oda.com/no/recipes/5036/",
    )


@pytest.mark.parametrize("via_url", [False, True])
async def test_import_structuring_preserves_each_explicit_source_measure(monkeypatch, via_url):
    imported = source_recipe()
    fetch = AsyncMock(return_value=imported)
    monkeypatch.setattr("home_kitchen.recipes.providers.imports.import_recipe", fetch)
    rows = [
        {"name": name, "quantity": None, "unit": None, "source_indices": [index]}
        for index, name in enumerate(["Salt", "Salt", "Pepper", "Pepper", "Kruspersille"])
    ]
    request = (
        RecipeDraftRequest(url=imported.source_url)
        if via_url
        else RecipeDraftRequest(imported=imported)
    )
    result = await provider_for(rows).draft("ai", request, RecipeContext(household=Household()))
    adapted = result.adapted
    assert adapted is not None
    assert [item.quantity for item in adapted.ingredients] == [Decimal("1")] * 5
    assert [item.unit for item in adapted.ingredients] == ["klype"] * 4 + ["håndfull"]
    assert [item.source_indices for item in adapted.ingredients] == [[i] for i in range(5)]
    assert [item.text for item in adapted.source_ingredients] == imported.ingredients
    assert adapted.servings == 4
    assert adapted.provenance.original_source == "imported"
    assert adapted.provenance.source_url == imported.source_url
    assert fetch.await_count == int(via_url)


@pytest.mark.parametrize("source_kind", ["prompt", "draft", "recipe"])
async def test_preservation_never_applies_to_adaptation_or_other_source_kinds(source_kind):
    imported = source_recipe()
    rows = [{"name": "Salt", "quantity": None, "unit": None, "source_indices": [0]}]
    if source_kind == "prompt":
        request = RecipeDraftRequest(imported=imported, prompt="Use less salt")
    elif source_kind == "draft":
        request = RecipeDraftRequest(
            draft=EditableRecipeDraft(
                title=imported.title,
                servings=4,
                ingredients=rows,
                instructions=["Cook"],
                source_ingredients=[{"index": 0, "text": "1klypeSalt"}],
            )
        )
    else:
        request = RecipeDraftRequest(
            recipe={
                "title": imported.title,
                "servings": 4,
                "ingredients": [{"name": "Salt", "quantity": 1, "unit": "klype"}],
                "instructions": ["Cook"],
            }
        )
    result = await provider_for(rows).draft("ai", request, RecipeContext(household=Household()))
    assert result.adapted.ingredients[0].quantity is None
    assert result.adapted.ingredients[0].unit is None


async def test_preservation_requires_missing_pair_and_single_source_reference():
    rows = [
        {"name": "Salt", "quantity": 2, "unit": "g", "source_indices": [0]},
        {"name": "Salt", "quantity": None, "unit": "g", "source_indices": [1]},
        {"name": "Pepper", "quantity": 2, "unit": None, "source_indices": [2]},
        {"name": "Pepper", "quantity": None, "unit": None, "source_indices": [2, 3]},
        {"name": "Herbs", "quantity": None, "unit": None, "source_indices": []},
    ]
    result = await provider_for(rows).draft(
        "ai", RecipeDraftRequest(imported=source_recipe()), RecipeContext(household=Household())
    )
    assert [(item.quantity, item.unit) for item in result.adapted.ingredients] == [
        (Decimal("2"), "g"),
        (None, "g"),
        (Decimal("2"), None),
        (None, None),
        (None, None),
    ]
