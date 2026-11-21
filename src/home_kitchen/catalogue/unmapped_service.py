"""Application service for listing and manually mapping unmapped recipe ingredients."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from home_kitchen.catalogue.identity import UnmappedIngredient
from home_kitchen.models import Recipe, RecipeCreate


class UnmappedRecipePort(Protocol):
    """Narrow port for reading and updating recipes during ingredient identity mapping."""

    def get_recipe(self, recipe_id: UUID) -> Recipe: ...

    def list_recipes(self) -> list[Recipe]: ...

    def update_recipe(self, recipe_id: UUID, data: RecipeCreate) -> Recipe: ...


class UnmappedIdentityPort(Protocol):
    """Narrow port for validating canonical identities and recording reviewed aliases."""

    def validate_identities(self, kind: str, ids: set[UUID]) -> None: ...

    def add_alias(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        identity_id: UUID,
        *,
        household_id: UUID | None = None,
        source_type: Any = "automatic_suggestion",
    ) -> None: ...


class UnmappedIngredientService:
    """Port-based application service for inspecting and mapping unmapped recipe ingredients."""

    def __init__(
        self,
        recipes: UnmappedRecipePort,
        identities: UnmappedIdentityPort,
        *,
        household_id: UUID | None = None,
    ) -> None:
        self.recipes = recipes
        self.identities = identities
        self.household_id = household_id

    def list_unmapped_ingredients(self, recipe_id: UUID | None = None) -> list[UnmappedIngredient]:
        recipes = [self.recipes.get_recipe(recipe_id)] if recipe_id else self.recipes.list_recipes()
        unmapped: list[UnmappedIngredient] = []
        for recipe in recipes:
            for index, ingredient in enumerate(recipe.ingredients):
                needs_product = ingredient.canonical_product_id is None
                needs_unit = bool(ingredient.unit) and ingredient.canonical_unit_id is None
                if needs_product or needs_unit:
                    unmapped.append(
                        UnmappedIngredient(
                            recipe_id=recipe.id,
                            recipe_title=recipe.title,
                            ingredient_index=index,
                            name=ingredient.name,
                            quantity=ingredient.quantity,
                            unit=ingredient.unit,
                            source_unit_expression=ingredient.source_unit_expression,
                            canonical_product_id=ingredient.canonical_product_id,
                            canonical_unit_id=ingredient.canonical_unit_id,
                        )
                    )
        return unmapped

    def map_unmapped_ingredient(
        self,
        recipe_id: UUID,
        ingredient_index: int,
        canonical_product_id: UUID | None = None,
        canonical_unit_id: UUID | None = None,
        *,
        add_alias: bool = False,
        locale: str = "nb",
        context: str = "recipe",
    ) -> Recipe:
        recipe = self.recipes.get_recipe(recipe_id)
        if ingredient_index < 0 or ingredient_index >= len(recipe.ingredients):
            raise IndexError("Ingredient index out of range")
        if canonical_product_id is None and canonical_unit_id is None:
            raise ValueError("Must specify canonical_product_id or canonical_unit_id to map")

        target = recipe.ingredients[ingredient_index]
        if canonical_product_id is not None:
            self.identities.validate_identities("product", {canonical_product_id})
            if add_alias:
                try:
                    self.identities.add_alias(
                        "product",
                        locale,
                        context,
                        target.name,
                        canonical_product_id,
                        household_id=self.household_id,
                        source_type="reviewed_correction",
                    )
                except TypeError:
                    self.identities.add_alias(
                        "product", locale, context, target.name, canonical_product_id
                    )
        if canonical_unit_id is not None:
            self.identities.validate_identities("unit", {canonical_unit_id})
            if add_alias and target.unit:
                try:
                    self.identities.add_alias(
                        "unit",
                        locale,
                        context,
                        target.unit,
                        canonical_unit_id,
                        household_id=self.household_id,
                        source_type="reviewed_correction",
                    )
                except TypeError:
                    self.identities.add_alias(
                        "unit", locale, context, target.unit, canonical_unit_id
                    )

        updated_ingredients = []
        for idx, ing in enumerate(recipe.ingredients):
            if idx == ingredient_index:
                updates: dict[str, Any] = {}
                if canonical_product_id is not None:
                    updates["canonical_product_id"] = canonical_product_id
                if canonical_unit_id is not None:
                    updates["canonical_unit_id"] = canonical_unit_id
                updated_ingredients.append(ing.model_copy(update=updates))
            else:
                updated_ingredients.append(ing)

        recipe_create = RecipeCreate(
            **recipe.model_dump(exclude={"id", "ingredients"}),
            ingredients=updated_ingredients,
        )
        return self.recipes.update_recipe(recipe_id, recipe_create)
