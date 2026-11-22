"""Application service orchestrating planned meal-agenda CRUD and scaled meal cooking views."""

from __future__ import annotations

from datetime import date, time
from uuid import UUID, uuid4

from home_kitchen.models import Meal, MealCreate, MealUpdate, PlannedMealRecipe
from home_kitchen.planning.domain.rules import validate_date_range
from home_kitchen.planning.ports.readers import SingleRecipeReader
from home_kitchen.planning.ports.writers import MealAgendaRepository
from home_kitchen.units import scale_quantity


class MealAgendaService:
    """Cohesive application service for managing planned meals and scaled recipe views."""

    def __init__(self, meals: MealAgendaRepository, recipes: SingleRecipeReader) -> None:
        self.meals = meals
        self.recipes = recipes

    def plan_meal(self, data: MealCreate) -> Meal:
        """Validate the referenced recipe exists and persist a new planned meal."""
        self.recipes.get_recipe(data.recipe_id)
        meal = Meal(id=uuid4(), **data.model_dump())
        self.meals.save_meal(meal)
        return meal

    def update_meal(self, meal_id: UUID, data: MealUpdate) -> Meal:
        """Update an existing planned meal after validating recipe and meal existence."""
        if not isinstance(data, MealUpdate):
            raise ValueError("expected_version is required when updating a meal")
        self.recipes.get_recipe(data.recipe_id)
        self.meals.get_meal(meal_id)
        meal = Meal(id=meal_id, **data.model_dump(exclude={"expected_version"}))
        return self.meals.update_meal(meal, expected_version=data.expected_version)

    def get_meal(self, meal_id: UUID) -> Meal:
        """Return one meal so clients can reload after a stale edit conflict."""
        return self.meals.get_meal(meal_id)

    def list_meals(self, start: date, end: date) -> list[Meal]:
        """List planned meals within [start, end] preserving insertion order for untimed items."""
        validate_date_range(start, end)
        return sorted(
            (meal for meal in self.meals.list_meals() if start <= meal.date <= end),
            key=lambda meal: (meal.date, meal.scheduled_time or time.max),
        )

    def meal_recipe(self, meal_id: UUID) -> PlannedMealRecipe:
        """Return a planned meal with ingredient quantities scaled to the meal's servings."""
        meal = self.meals.get_meal(meal_id)
        recipe = self.recipes.get_recipe(meal.recipe_id)
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

    def delete_meal(self, meal_id: UUID) -> None:
        """Remove a planned meal by ID."""
        self.meals.delete_meal(meal_id)
