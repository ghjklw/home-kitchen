"""In-memory secondary adapters for pure unit testing of recipe services."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from home_kitchen.models import Recipe, RecipeImageAttribution
from home_kitchen.recipes.ports import CollectionSession, GenerationLockableProvider


class MemoryCollectionSession:
    """Transactional in-memory session implementing `CollectionSession`."""

    def __init__(
        self,
        recipes_by_id: dict[UUID, Recipe],
        source_links: dict[tuple[str, str], dict[str, Any]],
        history_entries: list[dict[str, Any]],
        lock_calls: list[tuple[str, str]],
        history_hook: Any = None,
    ) -> None:
        self._recipes = recipes_by_id
        self._source_links = source_links
        self._history_entries = history_entries
        self._lock_calls = lock_calls
        self._history_hook = history_hook

    def acquire_source_lock(self, provider: str, external_recipe_id: str) -> None:
        self._lock_calls.append((provider, external_recipe_id))

    def lock_provider_generation(
        self, provider: GenerationLockableProvider, actor_id: str, generation: object
    ) -> None:
        provider.lock_generation(None, actor_id, generation)

    def find_existing_recipe_id(
        self, provider: str, external_recipe_id: str, source_url: str | None
    ) -> UUID | None:
        link = self._source_links.get((provider, external_recipe_id))
        if link is not None:
            return link["recipe_id"]
        if source_url is None:
            return None
        for recipe_id, recipe in self._recipes.items():
            if recipe.source_url is not None and str(recipe.source_url) == source_url:
                return recipe_id
        return None

    def save_recipe(self, recipe: Recipe) -> None:
        self._recipes[recipe.id] = recipe.model_copy(deep=True)

    def record_import_history(
        self,
        *,
        actor_id: str,
        recipe_id: UUID,
        provider: str,
        external_recipe_id: str,
        source_url: str | None,
        badges: list[str],
        message: str,
    ) -> None:
        entry = {
            "actor_id": actor_id,
            "recipe_id": recipe_id,
            "provider": provider,
            "external_recipe_id": external_recipe_id,
            "source_url": source_url,
            "badges": list(badges),
            "message": message,
        }
        if self._history_hook is not None:
            self._history_hook(entry)
        self._history_entries.append(entry)

    def link_source(
        self,
        *,
        provider: str,
        external_recipe_id: str,
        recipe_id: UUID,
        source_url: str | None,
        metadata: dict[str, Any],
    ) -> None:
        key = (provider, external_recipe_id)
        if key not in self._source_links:
            self._source_links[key] = {
                "provider": provider,
                "external_recipe_id": external_recipe_id,
                "recipe_id": recipe_id,
                "source_url": source_url,
                "metadata": dict(metadata),
                "created_at": datetime.now(UTC),
            }


class MemoryCollectionUnitOfWork:
    """In-memory unit of work with copy-on-write rollback semantics."""

    def __init__(self) -> None:
        self.recipes: dict[UUID, Recipe] = {}
        self.source_links: dict[tuple[str, str], dict[str, Any]] = {}
        self.history_entries: list[dict[str, Any]] = []
        self.lock_calls: list[tuple[str, str]] = []
        self.history_hook: Any = None

    def find_existing_recipe_id(
        self, provider: str, external_recipe_id: str, source_url: str | None
    ) -> UUID | None:
        link = self.source_links.get((provider, external_recipe_id))
        if link is not None:
            return link["recipe_id"]
        if source_url is None:
            return None
        for recipe_id, recipe in self.recipes.items():
            if recipe.source_url is not None and str(recipe.source_url) == source_url:
                return recipe_id
        return None

    @contextmanager
    def session(self) -> Generator[CollectionSession]:
        staged_recipes = {k: v.model_copy(deep=True) for k, v in self.recipes.items()}
        staged_links = {k: dict(v) for k, v in self.source_links.items()}
        staged_history = [dict(item) for item in self.history_entries]
        staged_locks = list(self.lock_calls)
        yield MemoryCollectionSession(
            staged_recipes,
            staged_links,
            staged_history,
            staged_locks,
            history_hook=self.history_hook,
        )
        self.recipes = staged_recipes
        self.source_links = staged_links
        self.history_entries = staged_history
        self.lock_calls = staged_locks


class MemoryRecipeImageRepository:
    """In-memory repository implementing `RecipeImageRepository` for unit tests."""

    def __init__(self, initial_recipes: list[Recipe] | None = None) -> None:
        self.recipes: dict[UUID, Recipe] = {
            recipe.id: recipe.model_copy(deep=True) for recipe in (initial_recipes or [])
        }
        self.history_entries: list[dict[str, Any]] = []

    def save_selected_image(
        self,
        *,
        actor_id: str,
        recipe_id: UUID,
        image_url: str,
        attribution: RecipeImageAttribution,
        provider: str,
        provider_id: str,
        source_page_url: str,
        license_name: str,
        license_url: str,
    ) -> Recipe:
        target_id = UUID(str(recipe_id))
        existing = self.recipes.get(target_id)
        if existing is None:
            raise KeyError(str(recipe_id))
        updated = existing.model_copy(
            update={"image_url": image_url, "image_attribution": attribution}
        )
        self.recipes[target_id] = updated
        self.history_entries.append(
            {
                "actor_id": actor_id,
                "recipe_id": target_id,
                "provider": provider,
                "provider_id": provider_id,
                "source_page_url": source_page_url,
                "license_name": license_name,
                "license_url": license_url,
            }
        )
        return updated.model_copy(deep=True)
