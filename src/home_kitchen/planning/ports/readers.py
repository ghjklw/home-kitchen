"""Port for reading recipe domain entities."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from home_kitchen.models import Recipe


class RecipeReader(Protocol):
    """Secondary port for loading recipes with optional concurrency locking."""

    def get_recipes(self, recipe_ids: list[UUID], *, lock: bool = False) -> dict[UUID, Recipe]: ...


class SingleRecipeReader(Protocol):
    """Secondary port for loading a single recipe by ID."""

    def get_recipe(self, recipe_id: UUID) -> Recipe: ...
