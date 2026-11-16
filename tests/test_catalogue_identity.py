"""Canonical identity reuse and conservative alias resolution."""

import os
from uuid import UUID, uuid4

import pytest

from home_kitchen.catalogue.identity import CanonicalProduct, CanonicalUnit
from home_kitchen.catalogue.identity_service import CanonicalIdentityService


class MemoryIdentityRepository:
    def __init__(self):
        self.products: dict[tuple[str, str], CanonicalProduct] = {}
        self.units: dict[str, CanonicalUnit] = {}
        self.aliases: dict[tuple[str, str, str, str], set[UUID]] = {}
        self.reviewed: set[tuple[str, str, str, str]] = set()

    def product(self, name: str, variant: str) -> CanonicalProduct:
        return self.products.setdefault(
            (name, variant), CanonicalProduct(id=uuid4(), canonical_name=name, variant=variant)
        )

    def unit(self, name: str) -> CanonicalUnit:
        return self.units.setdefault(name, CanonicalUnit(id=uuid4(), canonical_name=name))

    def add_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        key = kind, locale, context, " ".join(alias.split()).casefold()
        self.aliases.setdefault(key, set()).add(identity_id)

    def correct_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        key = kind, locale, context, " ".join(alias.split()).casefold()
        self.aliases[key] = {identity_id}
        self.reviewed.add(key)

    def resolve(
        self, kind: str, locale: str, context: str, alias: str
    ) -> list[CanonicalProduct | CanonicalUnit]:
        normalized = " ".join(alias.split()).casefold()
        ids = self.aliases.get((kind, locale, context, normalized), set()) | self.aliases.get(
            (kind, locale, "", normalized), set()
        )
        identities = (
            list(self.products.values()) if kind == "product" else list(self.units.values())
        )
        return [identity for identity in identities if identity.id in ids]


def test_product_identities_reuse_exact_canonical_variant_and_keep_variants_distinct():
    service = CanonicalIdentityService(MemoryIdentityRepository())
    first = service.create_product("  Red Tomato ")
    reused = service.create_product("red   tomato")
    distinct = service.create_product("red tomato", "canned")
    assert first.id == reused.id
    assert distinct.id != first.id


def test_aliases_are_language_and_context_scoped_and_ambiguity_needs_reviewed_correction():
    repository = MemoryIdentityRepository()
    service = CanonicalIdentityService(repository)
    fresh = service.create_product("tomato", "fresh")
    tinned = service.create_product("tomato", "tinned")
    service.add_alias("product", "nb", "recipe", "tomat", fresh.id)
    service.add_alias("product", "nb", "recipe", "tomat", tinned.id)

    assert service.resolve("product", "nb", "recipe", "tomat").status == "ambiguous"
    assert service.resolve("product", "en", "recipe", "tomat").status == "unmapped"
    corrected = service.correct_alias("product", "nb", "recipe", "tomat", fresh.id)
    assert corrected is None
    result = service.resolve("product", "nb", "recipe", " TOMAT ")
    assert result.status == "resolved"
    assert result.candidates == [fresh]
    assert ("product", "nb", "recipe", "tomat") in repository.reviewed


def test_unmapped_legacy_ingredient_remains_valid_and_source_quantity_is_unchanged():
    from decimal import Decimal

    from home_kitchen.models import RecipeCreate

    recipe = RecipeCreate.model_validate(
        {
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {"name": "my grandmother's odd ingredient", "quantity": "1.25", "unit": "handfuls"}
            ],
            "instructions": ["Cook"],
        }
    )
    assert recipe.ingredients[0].name == "my grandmother's odd ingredient"
    assert recipe.ingredients[0].quantity == Decimal("1.25")
    assert recipe.ingredients[0].unit == "handfuls"


def test_old_client_update_preserves_exact_unchanged_unique_ingredient_subdocument():
    from home_kitchen.models import Recipe, RecipeCreate
    from home_kitchen.service import KitchenService

    product, unit = uuid4(), uuid4()
    existing = Recipe.model_validate(
        {
            "id": uuid4(),
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {
                    "name": "pepper",
                    "quantity": "1",
                    "unit": "tsp",
                    "canonical_product_id": product,
                    "canonical_unit_id": unit,
                    "source_unit_expression": "Teaspoons",
                },
            ],
            "instructions": ["Cook"],
        }
    )
    old_payload = RecipeCreate.model_validate(
        {
            "title": "Soup edited",
            "servings": 2,
            "ingredients": [{"name": "pepper", "quantity": "1", "unit": "tsp"}],
            "instructions": ["Cook"],
        }
    )
    updated = KitchenService._retain_omitted_ingredient_identity_fields(
        old_payload, existing, [item.model_dump() for item in old_payload.ingredients]
    )
    assert updated[0]["canonical_product_id"] == product
    assert updated[0]["canonical_unit_id"] == unit
    assert updated[0]["source_unit_expression"] == "Teaspoons"


def test_old_client_update_signature_supports_nested_purchase_estimates():
    from home_kitchen.models import PurchaseEstimate, Recipe, RecipeCreate
    from home_kitchen.service import KitchenService

    product, unit = uuid4(), uuid4()
    estimate = PurchaseEstimate(
        quantity="100", unit="g", approximate=False, provenance="source estimate"
    )
    existing = Recipe.model_validate(
        {
            "id": uuid4(),
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {
                    "name": "pepper",
                    "quantity": "1",
                    "unit": "tsp",
                    "purchase_estimate": estimate.model_dump(),
                    "canonical_product_id": product,
                    "canonical_unit_id": unit,
                    "source_unit_expression": "Teaspoons",
                }
            ],
            "instructions": ["Cook"],
        }
    )
    old_payload = RecipeCreate.model_validate(
        {
            "title": "Soup edited",
            "servings": 2,
            "ingredients": [
                {
                    "name": "pepper",
                    "quantity": "1",
                    "unit": "tsp",
                    "purchase_estimate": estimate.model_dump(),
                }
            ],
            "instructions": ["Cook"],
        }
    )
    current_values = [item.model_dump() for item in old_payload.ingredients]
    updated = KitchenService._retain_omitted_ingredient_identity_fields(
        old_payload, existing, current_values
    )
    assert updated[0]["canonical_product_id"] == product
    assert updated[0]["canonical_unit_id"] == unit


def test_unmapped_quantity_edit_keeps_distinct_source_unit_wording_without_blocking_changes():
    from home_kitchen.models import Recipe, RecipeCreate
    from home_kitchen.service import KitchenService

    existing = Recipe.model_validate(
        {
            "id": uuid4(),
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {
                    "name": "pepper",
                    "quantity": "1",
                    "unit": "tbsp",
                    "source_unit_expression": "Tablespoons",
                }
            ],
            "instructions": ["Cook"],
        }
    )
    quantity_edit = RecipeCreate.model_validate(
        {
            "title": "Soup edited",
            "servings": 2,
            "ingredients": [{"name": "pepper", "quantity": "2", "unit": "tbsp"}],
            "instructions": ["Cook"],
        }
    )
    quantity_values = [item.model_dump() for item in quantity_edit.ingredients]
    quantity_result = KitchenService._retain_omitted_ingredient_identity_fields(
        quantity_edit, existing, quantity_values
    )
    assert quantity_result[0]["quantity"] == 2
    assert quantity_result[0]["source_unit_expression"] == "Tablespoons"

    measure_edit = RecipeCreate.model_validate(
        {
            "title": "Soup edited",
            "servings": 2,
            "ingredients": [{"name": "pepper", "quantity": "30", "unit": "ml"}],
            "instructions": ["Cook"],
        }
    )
    measure_values = [item.model_dump() for item in measure_edit.ingredients]
    measure_result = KitchenService._retain_omitted_ingredient_identity_fields(
        measure_edit, existing, measure_values
    )
    assert measure_result[0]["quantity"] == 30
    assert measure_result[0]["unit"] == "ml"
    assert measure_result[0]["source_unit_expression"] == "ml"


@pytest.mark.parametrize(
    "incoming_ingredients",
    [
        [
            {"name": "pepper", "quantity": "2", "unit": "tsp"},
            {"name": "salt", "quantity": "1", "unit": "tsp"},
        ],
        [
            {"name": "salt", "quantity": "1", "unit": "tsp"},
            {"name": "pepper", "quantity": "1", "unit": "tsp"},
        ],
        [
            {"name": "pepper", "quantity": "1", "unit": "tsp"},
            {"name": "pepper", "quantity": "1", "unit": "tsp"},
        ],
    ],
    ids=["changed", "reordered", "ambiguous-duplicate"],
)
def test_old_client_update_rejects_changed_reordered_or_ambiguous_rows(incoming_ingredients):
    from home_kitchen.models import Recipe, RecipeCreate
    from home_kitchen.service import KitchenService

    product, unit = uuid4(), uuid4()
    existing = Recipe.model_validate(
        {
            "id": uuid4(),
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {
                    "name": "pepper",
                    "quantity": "1",
                    "unit": "tsp",
                    "canonical_product_id": product,
                    "canonical_unit_id": unit,
                    "source_unit_expression": "Teaspoons",
                },
                {"name": "salt", "quantity": "1", "unit": "tsp"},
            ],
            "instructions": ["Cook"],
        }
    )
    old_payload = RecipeCreate.model_validate(
        {
            "title": "Soup edited",
            "servings": 2,
            "ingredients": incoming_ingredients,
            "instructions": ["Cook"],
        }
    )
    with pytest.raises(ValueError, match="include identity metadata"):
        KitchenService._retain_omitted_ingredient_identity_fields(
            old_payload, existing, [item.model_dump() for item in old_payload.ingredients]
        )


@pytest.mark.skipif(
    not os.environ.get("KITCHEN_TEST_DATABASE_URL"),
    reason="requires the explicitly configured disposable PostgreSQL test database",
)
def test_identity_api_reuses_products_resolves_aliases_and_preserves_recipe_payload(
    test_database_url,
):
    from fastapi.testclient import TestClient

    from home_kitchen.app import create_app
    from home_kitchen.settings import Settings

    with TestClient(
        create_app(Settings(local_dev=True, database_url=test_database_url)),
        client=("127.0.0.1", 5000),
    ) as client:
        first = client.post(
            "/api/catalogue/identities/products",
            json={"canonical_name": "Tomato", "variant": "fresh"},
        )
        reused = client.post(
            "/api/catalogue/identities/products",
            json={"canonical_name": " tomato ", "variant": "FRESH"},
        )
        distinct = client.post(
            "/api/catalogue/identities/products",
            json={"canonical_name": "tomato", "variant": "canned"},
        )
        assert first.status_code == reused.status_code == distinct.status_code == 201
        assert first.json()["id"] == reused.json()["id"]
        assert distinct.json()["id"] != first.json()["id"]

        alias_path = f"/api/catalogue/aliases/product/{first.json()['id']}"
        assert (
            client.post(
                alias_path, json={"alias": "tomat", "locale": "nb", "context": "recipe"}
            ).status_code
            == 201
        )
        assert (
            client.post(
                f"/api/catalogue/aliases/product/{distinct.json()['id']}",
                json={"alias": "tomat", "locale": "nb", "context": "recipe"},
            ).status_code
            == 201
        )
        ambiguous = client.get(
            "/api/catalogue/aliases/product",
            params={"alias": "tomat", "locale": "nb", "context": "recipe"},
        )
        assert ambiguous.json()["status"] == "ambiguous"
        assert (
            client.put(
                f"{alias_path}/review", json={"alias": "tomat", "locale": "nb", "context": "recipe"}
            ).status_code
            == 204
        )
        resolved = client.get(
            "/api/catalogue/aliases/product",
            params={"alias": "tomat", "locale": "nb", "context": "recipe"},
        )
        assert resolved.json()["status"] == "resolved"
        assert resolved.json()["candidates"][0]["id"] == first.json()["id"]

        unit = client.post("/api/catalogue/identities/units", json={"canonical_name": "gram"})
        assert unit.status_code == 201
        unit_alias_path = f"/api/catalogue/aliases/unit/{unit.json()['id']}"
        assert (
            client.post(
                unit_alias_path,
                json={"alias": "g", "locale": "nb", "context": "recipe"},
            ).status_code
            == 201
        )
        unit_result = client.get(
            "/api/catalogue/aliases/unit",
            params={"alias": "g", "locale": "nb", "context": "recipe"},
        )
        assert unit_result.json()["status"] == "resolved"
        assert unit_result.json()["candidates"][0]["id"] == unit.json()["id"]

        nonexistent_id = "00000000-0000-0000-0000-000000000001"
        for path in (
            f"/api/catalogue/aliases/product/{unit.json()['id']}",
            f"/api/catalogue/aliases/unit/{nonexistent_id}",
        ):
            invalid_alias = client.post(
                path, json={"alias": "bad", "locale": "nb", "context": "recipe"}
            )
            assert invalid_alias.status_code == 422

        recipe = client.post(
            "/api/recipes",
            json={
                "title": "Legacy",
                "servings": 1,
                "ingredients": [
                    {
                        "name": "Old unmatched phrase",
                        "quantity": "1.25",
                        "unit": "Handfuls",
                        "canonical_product_id": first.json()["id"],
                        "canonical_unit_id": unit.json()["id"],
                    }
                ],
                "instructions": ["Cook"],
            },
        )
        assert recipe.status_code == 201
        assert recipe.json()["ingredients"][0]["quantity"] == "1.25"
        assert recipe.json()["ingredients"][0]["unit"] == "handfuls"
        assert recipe.json()["ingredients"][0]["source_unit_expression"] == "Handfuls"
        assert recipe.json()["ingredients"][0]["canonical_product_id"] == first.json()["id"]
        assert recipe.json()["ingredients"][0]["canonical_unit_id"] == unit.json()["id"]

        old_client_unchanged_update = client.put(
            f"/api/recipes/{recipe.json()['id']}",
            json={
                "title": "Legacy edited",
                "servings": 1,
                "ingredients": [
                    {"name": "Old unmatched phrase", "quantity": "1.25", "unit": "handfuls"}
                ],
                "instructions": ["Cook slowly"],
            },
        )
        assert old_client_unchanged_update.status_code == 200
        edited_ingredient = old_client_unchanged_update.json()["ingredients"][0]
        assert edited_ingredient["quantity"] == "1.25"
        assert edited_ingredient["canonical_product_id"] == first.json()["id"]
        assert edited_ingredient["canonical_unit_id"] == unit.json()["id"]
        assert edited_ingredient["source_unit_expression"] == "Handfuls"

        old_client_changed_update = client.put(
            f"/api/recipes/{recipe.json()['id']}",
            json={
                "title": "Legacy edited",
                "servings": 1,
                "ingredients": [
                    {"name": "Old unmatched phrase", "quantity": "2.5", "unit": "handfuls"}
                ],
                "instructions": ["Cook slowly"],
            },
        )
        assert old_client_changed_update.status_code == 422
        assert "include identity metadata" in old_client_changed_update.json()["detail"]
        after_rejection = client.get(f"/api/recipes/{recipe.json()['id']}").json()["ingredients"][0]
        assert after_rejection["quantity"] == "1.25"
        assert after_rejection["canonical_product_id"] == first.json()["id"]

        explicitly_cleared = client.put(
            f"/api/recipes/{recipe.json()['id']}",
            json={
                "title": "Legacy edited",
                "servings": 1,
                "ingredients": [
                    {
                        "name": "Old unmatched phrase",
                        "quantity": "2.5",
                        "unit": "handfuls",
                        "canonical_product_id": None,
                        "canonical_unit_id": None,
                        "source_unit_expression": None,
                    }
                ],
                "instructions": ["Cook slowly"],
            },
        )
        assert explicitly_cleared.status_code == 200
        cleared_ingredient = explicitly_cleared.json()["ingredients"][0]
        assert cleared_ingredient["canonical_product_id"] is None
        assert cleared_ingredient["canonical_unit_id"] is None
        assert cleared_ingredient["source_unit_expression"] is None
        invalid_recipe = client.post(
            "/api/recipes",
            json={
                "title": "Invalid link",
                "servings": 1,
                "ingredients": [
                    {
                        "name": "Tomato",
                        "quantity": "1",
                        "unit": "item",
                        "canonical_product_id": nonexistent_id,
                    }
                ],
                "instructions": ["Cook"],
            },
        )
        assert invalid_recipe.status_code == 422
        invalid_update = client.put(
            f"/api/recipes/{recipe.json()['id']}",
            json={
                "title": "Invalid update",
                "servings": 1,
                "ingredients": [
                    {
                        "name": "Tomato",
                        "quantity": "1",
                        "unit": "item",
                        "canonical_unit_id": first.json()["id"],
                    }
                ],
                "instructions": ["Cook"],
            },
        )
        assert invalid_update.status_code == 422
