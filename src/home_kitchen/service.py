"""Household recipes, meal planning, and deliberate shopping suggestions."""

from datetime import date
from uuid import UUID, uuid4

from .models import (
    Meal,
    MealCreate,
    PlannedMealRecipe,
    Recipe,
    RecipeCreate,
    RegularItem,
    ShoppingItem,
    ShoppingItemCreate,
    WeekShopping,
)
from .shopping import ShoppingBuilder, ShoppingContext, default_shopping_builder
from .store import Store
from .units import scale_quantity


class KitchenService:
    def __init__(self, database_url: str, shopping_builder: ShoppingBuilder | None = None):
        self.store = Store(database_url)
        self.shopping_builder = shopping_builder or default_shopping_builder()

    def create_recipe(self, data: RecipeCreate) -> Recipe:
        # Image credit is written only by the authenticated candidate-selection flow.
        recipe = Recipe(id=uuid4(), **data.model_dump(exclude={"image_attribution"}))
        self.store.save("recipe", recipe)
        return recipe

    def list_recipes(self) -> list[Recipe]:
        return self.store.list("recipe", Recipe)

    def get_recipe(self, recipe_id: UUID) -> Recipe:
        return self.store.get("recipe", recipe_id, Recipe)

    def update_recipe(self, recipe_id: UUID, data: RecipeCreate) -> Recipe:
        existing = self.get_recipe(recipe_id)
        values = data.model_dump(exclude={"image_attribution"})
        values["image_attribution"] = (
            existing.image_attribution if data.image_url == existing.image_url else None
        )
        recipe = Recipe(id=recipe_id, **values)
        self.store.save("recipe", recipe)
        return recipe

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

    def list_meals(self, start: date, end: date) -> list[Meal]:
        self._validate_range(start, end)
        return sorted(
            (meal for meal in self.store.list("meal", Meal) if start <= meal.date <= end),
            key=lambda meal: (meal.date, meal.slot),
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
                        )
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
