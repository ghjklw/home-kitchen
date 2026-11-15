"""Household recipes, meal planning, and deliberate shopping suggestions."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from .models import (
    Meal,
    MealCreate,
    Recipe,
    RecipeCreate,
    RegularItem,
    ShoppingItem,
    ShoppingItemCreate,
    ShoppingLine,
    WeekShopping,
)
from .store import Store


class KitchenService:
    def __init__(self, db_path: Path):
        self.store = Store(db_path)

    def create_recipe(self, data: RecipeCreate) -> Recipe:
        recipe = Recipe(id=uuid4(), **data.model_dump())
        self.store.save("recipe", recipe)
        return recipe

    def list_recipes(self) -> list[Recipe]:
        return self.store.list("recipe", Recipe)

    def get_recipe(self, recipe_id: UUID) -> Recipe:
        return self.store.get("recipe", recipe_id, Recipe)

    def update_recipe(self, recipe_id: UUID, data: RecipeCreate) -> Recipe:
        self.get_recipe(recipe_id)
        recipe = Recipe(id=recipe_id, **data.model_dump())
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
        item = ShoppingItem(id=uuid4(), **regular.model_dump(include={"name", "quantity", "unit"}))
        return self.store.confirm(item_id, item)

    def shopping_list(self, start: date, end: date, today: date) -> WeekShopping:
        self._validate_range(start, end)
        lines: dict[tuple[str, str], ShoppingLine] = {}

        def add(name: str, quantity: Decimal, unit: str, origin: str) -> None:
            key = (name.strip().casefold(), unit.strip().casefold())
            if key not in lines:
                lines[key] = ShoppingLine(
                    name=name.strip(), quantity=quantity, unit=key[1], origins=[origin]
                )
            else:
                line = lines[key]
                line.quantity += quantity
                if origin not in line.origins:
                    line.origins.append(origin)

        for meal in self.list_meals(start, end):
            recipe = self.get_recipe(meal.recipe_id)
            for ingredient in recipe.ingredients:
                quantity = ingredient.quantity * Decimal(meal.servings) / Decimal(recipe.servings)
                add(ingredient.name, quantity, ingredient.unit, f"meal:{meal.id}")
        for item in self.list_items():
            if not item.completed:
                add(item.name, item.quantity, item.unit, f"item:{item.id}")
        confirmed = self.store.active_confirmations()
        suggestions = []
        for regular in self.list_regular_items():
            if regular.id in confirmed:
                continue
            if regular.last_purchased is None:
                add(regular.name, regular.quantity, regular.unit, f"regular:{regular.id}")
            elif (today - regular.last_purchased).days >= regular.interval_days:
                suggestions.append(regular)
        return WeekShopping(
            lines=sorted(lines.values(), key=lambda line: (line.name.casefold(), line.unit)),
            suggestions=suggestions,
        )

    @staticmethod
    def _validate_range(start: date, end: date) -> None:
        if end < start:
            raise ValueError("end must be on or after start")
