"""Port for reading and persisting confirmed meals."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from home_kitchen.models import Meal


class MealWriter(Protocol):
    """Secondary port for loading, inserting, and updating planned meals."""

    def get_meals(self, meal_ids: list[UUID], *, lock: bool = False) -> dict[UUID, Meal]: ...

    def insert_meal(self, meal: Meal) -> bool: ...

    def update_meal(self, meal: Meal) -> bool: ...


class MealAgendaRepository(Protocol):
    """Secondary port for CRUD persistence of planned meal agenda entries."""

    def save_meal(self, meal: Meal) -> None: ...

    def get_meal(self, meal_id: UUID) -> Meal: ...

    def update_meal(self, meal: Meal, *, expected_version: int) -> Meal: ...

    def list_meals(self) -> list[Meal]: ...

    def delete_meal(self, meal_id: UUID) -> None: ...

    def has_meals_for_recipe(self, recipe_id: UUID) -> bool: ...
