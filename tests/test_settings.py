"""Household settings round-trip without database or external service access."""

import pytest
from pydantic import ValidationError

from home_kitchen.settings import DEFAULT_SHOPPING_CATEGORY_ORDER, Household


def test_shopping_category_defaults_are_independent_and_cover_source_labels():
    first = Household()
    second = Household()
    assert first.language == "nb"
    assert first.shopping_category_order == list(DEFAULT_SHOPPING_CATEGORY_ORDER)
    assert set(first.shopping_category_order) == {
        "Produce",
        "Bakery",
        "Meat",
        "Seafood",
        "Fish",
        "Dairy",
        "Chilled",
        "Pantry",
        "Spices",
        "Drinks",
        "Frozen",
        "Household",
        "Other",
    }
    first.shopping_category_order.clear()
    assert second.shopping_category_order == list(DEFAULT_SHOPPING_CATEGORY_ORDER)


def test_custom_shopping_categories_are_trimmed_preserved_and_serialized():
    household = Household(
        name="Test household", shopping_category_order=["  Frukt og grønt  ", "Fish", "Seafood"]
    )
    assert household.shopping_category_order == ["Frukt og grønt", "Fish", "Seafood"]
    restored = Household.model_validate_json(household.model_dump_json())
    assert restored.shopping_category_order == household.shopping_category_order


def test_explicit_empty_shopping_category_order_is_preserved():
    assert (
        Household.model_validate({"shopping_category_order": []}).model_dump()[
            "shopping_category_order"
        ]
        == []
    )


@pytest.mark.parametrize(
    "categories",
    [
        [""],
        ["   "],
        ["Fish", " fish "],
        ["STRASSE", "Straße"],
        ["a" * 101],
        [f"Category {index}" for index in range(65)],
    ],
)
def test_shopping_category_order_rejects_blank_duplicate_and_unbounded_values(categories):
    with pytest.raises(ValidationError):
        Household(shopping_category_order=categories)
