"""Port for persisting confirmed meals."""

from __future__ import annotations

from typing import Protocol

from home_kitchen.models import Meal


class MealWriter(Protocol):
    """Secondary port for saving confirmed meals."""

    def insert_meal(self, meal: Meal) -> bool: ...
