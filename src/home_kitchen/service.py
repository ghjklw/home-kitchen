"""Household recipes, meal planning, and deliberate shopping suggestions."""

from datetime import date, time
from uuid import UUID, uuid4

from .models import (
    Meal,
    MealCreate,
    PlannedMealRecipe,
    PurchaseEstimate,
    Recipe,
    RecipeCreate,
    RegularItem,
    ShoppingItem,
    ShoppingItemCreate,
    WeekShopping,
)
from .recipes.source_quantities import normalize_source_quantity, source_ingredient_parts
from .shopping import ShoppingBuilder, ShoppingContext, default_shopping_builder
from .store import Store
from .units import scale_quantity

_PURCHASABLE_SOURCE_UNITS = frozenset({"g", "kg", "mg", "ml", "cl", "dl", "l", "stk", "item"})


class KitchenService:
    def __init__(self, database_url: str, shopping_builder: ShoppingBuilder | None = None):
        self.store = Store(database_url)
        self.shopping_builder = shopping_builder or default_shopping_builder()

    def create_recipe(self, data: RecipeCreate) -> Recipe:
        # Image credit is written only by the authenticated candidate-selection flow.
        recipe = Recipe(id=uuid4(), **self._recipe_values(data, exclude={"image_attribution"}))
        self.store.save("recipe", recipe)
        return recipe

    def list_recipes(self) -> list[Recipe]:
        return self.store.list("recipe", Recipe)

    def get_recipe(self, recipe_id: UUID) -> Recipe:
        return self.store.get("recipe", recipe_id, Recipe)

    def update_recipe(self, recipe_id: UUID, data: RecipeCreate) -> Recipe:
        existing = self.get_recipe(recipe_id)
        values = self._recipe_values(data, exclude={"image_attribution"})
        values["image_attribution"] = (
            existing.image_attribution if data.image_url == existing.image_url else None
        )
        recipe = Recipe(id=recipe_id, **values)
        self.store.save("recipe", recipe)
        return recipe

    @staticmethod
    def _recipe_values(data: RecipeCreate, *, exclude: set[str]) -> dict:
        """Derive purchase estimates only from explicitly linked source rows."""
        values = data.model_dump(exclude=exclude)
        source_rows = {row.index: row.text for row in data.source_ingredients}
        ingredients = []
        for ingredient in data.ingredients:
            item = ingredient.model_dump()
            estimate = None
            if len(ingredient.source_indices) == 1:
                source_text = source_rows.get(ingredient.source_indices[0])
                if source_text is not None:
                    source_name, parsed_source = source_ingredient_parts(source_text)
                    normalized_name = " ".join(ingredient.name.casefold().split())
                    linked_name = " ".join(source_name.casefold().split())
                    if normalized_name == linked_name and ingredient.unit not in {
                        "klype",
                        "håndfull",
                        "etter smak",
                    }:
                        quantity = ingredient.quantity or parsed_source.quantity
                        unit = ingredient.unit or parsed_source.unit
                        if quantity is not None and unit is not None:
                            parsed_current = normalize_source_quantity(
                                f"{quantity} {unit} {ingredient.name}", ingredient.name
                            )
                            if unit in {"klype", "håndfull", "etter smak"}:
                                parsed_current = None
                            if parsed_current is not None:
                                if parsed_current.purchase_estimate is not None:
                                    values_for_estimate = parsed_current.purchase_estimate.__dict__
                                elif unit in _PURCHASABLE_SOURCE_UNITS:
                                    values_for_estimate = {
                                        "quantity": quantity,
                                        "unit": unit,
                                        "approximate": False,
                                        "provenance": "Current recipe measure",
                                        "source_url": None,
                                    }
                                else:
                                    values_for_estimate = None
                                if values_for_estimate is not None:
                                    values_for_estimate = dict(values_for_estimate)
                                    values_for_estimate["provenance"] = (
                                        f"{values_for_estimate['provenance']}; "
                                        f"original source text: {source_text}"
                                    )
                                    estimate = PurchaseEstimate.model_validate(
                                        values_for_estimate
                                    ).model_dump()
            # Treat estimates as derived data: never accept an unsupported amount
            # supplied by a client or model, and keep the recipe's cooking measure.
            item["purchase_estimate"] = estimate
            ingredients.append(item)
        values["ingredients"] = ingredients
        return values

    def delete_recipe(self, recipe_id: UUID) -> None:
        self.get_recipe(recipe_id)
        if any(meal.recipe_id == recipe_id for meal in self.store.list("meal", Meal)):
            raise ValueError("Remove planned meals before deleting this recipe")
        self.store.delete("recipe", recipe_id)

    def plan_meal(self, data: MealCreate) -> Meal:
        self.get_recipe(data.recipe_id)
        meal = Meal(id=uuid4(), **data.model_dump())
        self.store.save("meal", meal)
        return meal

    def update_meal(self, meal_id: UUID, data: MealCreate) -> Meal:
        self.get_recipe(data.recipe_id)
        self.store.get("meal", meal_id, Meal)
        meal = Meal(id=meal_id, **data.model_dump())
        self.store.update_meal(meal)
        return meal

    def list_meals(self, start: date, end: date) -> list[Meal]:
        self._validate_range(start, end)
        return sorted(
            (meal for meal in self.store.list("meal", Meal) if start <= meal.date <= end),
            # Store.list is ordered by the persistent insertion position. The stable sort
            # keeps that agenda order for untimed entries and uses explicit times where set.
            key=lambda meal: (meal.date, meal.scheduled_time or time.max),
        )

    def meal_recipe(self, meal_id: UUID) -> PlannedMealRecipe:
        meal = self.store.get("meal", meal_id, Meal)
        recipe = self.get_recipe(meal.recipe_id)
        return PlannedMealRecipe(
            meal=meal,
            source_recipe=recipe,
            servings=meal.servings,
            ingredients=[
                ingredient.model_copy(
                    update={
                        "quantity": (
                            scale_quantity(ingredient.quantity, meal.servings, recipe.servings)
                            if ingredient.quantity is not None
                            else None
                        ),
                        "purchase_estimate": (
                            ingredient.purchase_estimate.model_copy(
                                update={
                                    "quantity": scale_quantity(
                                        ingredient.purchase_estimate.quantity,
                                        meal.servings,
                                        recipe.servings,
                                    )
                                }
                            )
                            if ingredient.purchase_estimate is not None
                            else None
                        ),
                    }
                )
                for ingredient in recipe.ingredients
            ],
        )

    def add_item(self, data: ShoppingItemCreate) -> ShoppingItem:
        item = ShoppingItem(id=uuid4(), **data.model_dump())
        self.store.save("item", item)
        return item

    def delete_meal(self, meal_id: UUID) -> None:
        self.store.delete("meal", meal_id)

    def delete_item(self, item_id: UUID) -> None:
        self.store.delete("item", item_id)

    def list_items(self) -> list[ShoppingItem]:
        return self.store.list("item", ShoppingItem)

    def complete_item(self, item_id: UUID, completed: bool) -> ShoppingItem:
        item = self.store.get("item", item_id, ShoppingItem)
        item.completed = completed
        self.store.save("item", item)
        return item

    def add_regular_item(self, data: ShoppingItemCreate, interval_days: int = 7) -> RegularItem:
        item = RegularItem(id=uuid4(), interval_days=interval_days, **data.model_dump())
        self.store.save("regular", item)
        return item

    def list_regular_items(self) -> list[RegularItem]:
        return self.store.list("regular", RegularItem)

    def record_purchase(self, item_id: UUID, purchased_on: date) -> RegularItem:
        item = self.store.get("regular", item_id, RegularItem)
        item.last_purchased = purchased_on
        self.store.save("regular", item)
        return item

    def confirm_suggestion(self, item_id: UUID) -> ShoppingItem:
        regular = self.store.get("regular", item_id, RegularItem)
        item = ShoppingItem(
            id=uuid4(), **regular.model_dump(include={"name", "quantity", "unit", "category"})
        )
        return self.store.confirm(item_id, item)

    def shopping_list(self, start: date, end: date, today: date) -> WeekShopping:
        self._validate_range(start, end)
        context = ShoppingContext(
            meals=self.list_meals(start, end),
            recipes={recipe.id: recipe for recipe in self.list_recipes()},
            captures=self.list_items(),
            regulars=self.list_regular_items(),
            active_confirmations=self.store.active_confirmations(),
            today=today,
        )
        return self.shopping_builder.build(context)

    @staticmethod
    def _validate_range(start: date, end: date) -> None:
        if end < start:
            raise ValueError("end must be on or after start")
