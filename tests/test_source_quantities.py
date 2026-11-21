from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.models.test import TestModel

from home_kitchen.ai import recipe_agent
from home_kitchen.imports import ImportedDraft
from home_kitchen.models import Meal, Recipe
from home_kitchen.recipes.models import EditableRecipeDraft, RecipeContext, RecipeDraftRequest
from home_kitchen.recipes.providers import create_recipe_providers
from home_kitchen.recipes.source_quantities import (
    MEASURE_SOURCE,
    explicit_source_measure,
    normalize_source_quantity,
    source_ingredient_parts,
)
from home_kitchen.settings import Household, Settings
from home_kitchen.shopping import PlannedRecipes, ShoppingContext

pytestmark = pytest.mark.mocked_provider


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
        ("1/2 ts salt", (Decimal("0.5"), "ts")),
        ("1 1/2 ss olje", (Decimal("1.5"), "ss")),
        ("½ ts salt", (Decimal("0.5"), "ts")),
        ("salt etter smak", None),
        ("en klype salt", None),
        ("1-2 ts salt", None),
        ("1 laks", None),
        ("1 pakke pasta", None),
        ("0 g salt", None),
    ],
)
def test_explicit_source_measure_is_conservative(text, expected):
    assert explicit_source_measure(text) == expected


@pytest.mark.parametrize(
    ("text", "ingredient", "quantity", "unit", "estimate", "estimate_unit", "approximate"),
    [
        ("1 klype salt", "salt", Decimal("1"), "klype", None, None, False),
        ("2 håndfull persille", "persille", Decimal("2"), "håndfull", None, None, False),
        ("salt etter smak", "salt", None, "etter smak", None, None, False),
        ("1/2 dl vann", "vann", Decimal("0.5"), "dl", None, None, False),
        ("1,5 kg epler", "epler", Decimal("1.5"), "kg", None, None, False),
        ("2 ss hvetemel", "hvetemel", Decimal("2"), "ss", Decimal("18"), "g", True),
        ("1 dl sukker", "sukker", Decimal("1"), "dl", Decimal("90"), "g", True),
        (
            "1 dl lettkokte havregryn",
            "lettkokte havregryn",
            Decimal("1"),
            "dl",
            Decimal("40"),
            "g",
            True,
        ),
    ],
)
def test_source_quantity_separates_cooking_measure_and_purchase_estimate(
    text, ingredient, quantity, unit, estimate, estimate_unit, approximate
):
    result = normalize_source_quantity(text, ingredient)
    assert (result.quantity, result.unit) == (quantity, unit)
    assert result.original_text == text
    if estimate is None:
        assert result.purchase_estimate is None
    else:
        assert result.purchase_estimate.quantity == estimate
        assert result.purchase_estimate.unit == estimate_unit
        assert result.purchase_estimate.approximate is approximate
        assert result.purchase_estimate.source_url == MEASURE_SOURCE
        assert "Matprat" in result.purchase_estimate.provenance


@pytest.mark.parametrize(
    "text",
    [
        "0 g salt",
        "1-2 ts salt",
        "1 pakke pasta",
        "mengde ukjent",
        "1/0 dl vann",
        "håndfull persille",
    ],
)
def test_unknown_or_invalid_source_amount_stays_unresolved(text):
    result = normalize_source_quantity(text, "hvetemel")
    assert result.quantity is None
    assert result.unit is None
    assert result.original_text == text


def test_handful_and_pinch_are_never_given_a_universal_gram_value():
    for text in ("1 håndfull persille", "1 klype salt"):
        result = normalize_source_quantity(text, "unmapped ingredient")
        assert result.quantity == Decimal("1")
        assert result.unit in {"håndfull", "klype"}
        assert result.approximate is False
        assert result.provenance == "source text"


def test_source_line_split_preserves_cooking_measure_and_source_expression():
    name, parsed = source_ingredient_parts("1 dl sukker")
    assert name == "sukker"
    assert (parsed.quantity, parsed.unit) == (Decimal("1"), "dl")
    assert parsed.purchase_estimate.quantity == Decimal("90")
    assert parsed.original_text == "1 dl sukker"

    name, parsed = source_ingredient_parts("1klypeSalt")
    assert name == "Salt"
    assert (parsed.quantity, parsed.unit) == (Decimal("1"), "klype")
    assert parsed.purchase_estimate is None


async def test_ai_import_estimate_survives_saved_recipe_model_and_planned_shopping():
    imported = ImportedDraft(
        title="Røre",
        servings=2,
        ingredients=["1 dl sukker", "2 ss hvetemel", "1 klype salt"],
        instructions=["Rør sammen"],
        source_url="https://matprat.no/oppskrifter/ror/",
    )
    rows = [
        {
            "name": name,
            "quantity": None,
            "unit": None,
            "source_indices": [index],
            "purchase_estimate": {
                "quantity": 999,
                "unit": "g",
                "approximate": True,
                "provenance": "model claim",
            },
        }
        for index, name in enumerate(["sukker", "hvetemel", "salt"])
    ]
    settings = Settings(local_dev=True)
    agent = recipe_agent(
        settings,
        TestModel(
            custom_output_args={
                "title": imported.title,
                "servings": imported.servings,
                "ingredients": rows,
                "instructions": imported.instructions,
                "source_ingredients": [
                    {"index": index, "text": text}
                    for index, text in enumerate(imported.ingredients)
                ],
            }
        ),
    )
    draft = await create_recipe_providers(settings, agent).draft(
        "ai", RecipeDraftRequest(imported=imported), RecipeContext(household=Household())
    )
    adapted = draft.adapted
    assert adapted is not None
    assert [(item.quantity, item.unit) for item in adapted.ingredients] == [
        (Decimal("1"), "dl"),
        (Decimal("2"), "ss"),
        (Decimal("1"), "klype"),
    ]
    assert [item.text for item in adapted.source_ingredients] == imported.ingredients
    assert adapted.ingredients[0].purchase_estimate.quantity == Decimal("90")
    assert adapted.ingredients[1].purchase_estimate.quantity == Decimal("18")
    assert adapted.ingredients[2].purchase_estimate is None

    recipe = Recipe.model_validate(
        {"id": "a550b7d6-cbe7-4d54-a9cb-8e92f8c72c10", **adapted.model_dump()}
    )
    shopping = PlannedRecipes().contribute(
        ShoppingContext(
            meals=[
                Meal(
                    id="a550b7d6-cbe7-4d54-a9cb-8e92f8c72c11",
                    date=date(2026, 11, 16),
                    recipe_id=recipe.id,
                    servings=2,
                )
            ],
            recipes={recipe.id: recipe},
            captures=[],
            regulars=[],
            active_confirmations=set(),
            today=date(2026, 11, 16),
        )
    )
    assert [(line.name, line.quantity, line.unit) for line in shopping.lines] == [
        ("sukker", Decimal("90"), "g"),
        ("hvetemel", Decimal("18"), "g"),
    ]
    assert all(line.approximate for line in shopping.lines)
    sugar_line = next(line for line in shopping.lines if line.name == "sukker")
    assert "1 dl sukker" in sugar_line.provenance
    assert str(sugar_line.source_url).startswith("https://www.matprat.no/")
    assert [(need.name, need.unit) for need in shopping.unquantified] == [("salt", "klype")]


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
