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
