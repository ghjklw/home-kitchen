"""In-memory secondary adapters for pure unit testing of recipe services."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from home_kitchen.models import (
    Recipe,
    RecipeActivityStats,
    RecipeImageAttribution,
    RecipeTranslation,
)
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


class MemoryRecipeRepository:
    """In-memory repository implementing recipe CRUD, translations, meal refs, stats, and locale."""

    def __init__(
        self,
        initial_recipes: list[Recipe] | None = None,
        *,
        household_locale: str = "nb",
        planned_recipe_ids: set[UUID] | None = None,
    ) -> None:
        self.recipes: dict[UUID, Recipe] = {
            recipe.id: recipe.model_copy(deep=True) for recipe in (initial_recipes or [])
        }
        self.translations: dict[tuple[UUID, str, str], RecipeTranslation] = {}
        self.stats: dict[UUID, RecipeActivityStats] = {}
        self.planned_recipe_ids: set[UUID] = set(planned_recipe_ids or ())
        self.household_locale = household_locale
        self.history_entries: list[dict[str, Any]] = []

    def save(self, recipe: Recipe) -> None:
        self.recipes[recipe.id] = recipe.model_copy(deep=True)
        self.history_entries.append(
            {"kind": "domain_event", "entity_type": "recipe", "recipe_id": recipe.id}
        )

    def get(self, recipe_id: UUID) -> Recipe:
        if recipe_id not in self.recipes:
            raise KeyError(str(recipe_id))
        return self.recipes[recipe_id].model_copy(deep=True)

    def list_all(self) -> list[Recipe]:
        return [recipe.model_copy(deep=True) for recipe in self.recipes.values()]

    def delete(self, recipe_id: UUID) -> None:
        if recipe_id not in self.recipes:
            raise KeyError(str(recipe_id))
        del self.recipes[recipe_id]

    def has_meals_for_recipe(self, recipe_id: UUID) -> bool:
        return recipe_id in self.planned_recipe_ids

    def save_translation(self, translation: RecipeTranslation) -> None:
        key = (translation.recipe_id, translation.target_locale, translation.recipe_version)
        self.translations[key] = translation.model_copy(deep=True)
        self.history_entries.append(
            {
                "kind": "domain_event",
                "entity_type": "recipe_translation",
                "recipe_id": translation.recipe_id,
                "target_locale": translation.target_locale,
            }
        )

    def get_translation(
        self, recipe_id: UUID, target_locale: str, recipe_version: str | None = None
    ) -> RecipeTranslation | None:
        matches = [
            tr
            for (rid, loc, ver), tr in self.translations.items()
            if rid == recipe_id
            and loc == target_locale
            and (recipe_version is None or ver == recipe_version)
        ]
        if not matches:
            return None
        matches.sort(key=lambda tr: tr.created_at, reverse=True)
        return matches[0].model_copy(deep=True)

    def list_translations(self, recipe_id: UUID) -> list[RecipeTranslation]:
        matches = [
            tr.model_copy(deep=True)
            for (rid, _, _), tr in self.translations.items()
            if rid == recipe_id
        ]
        matches.sort(key=lambda tr: tr.created_at, reverse=True)
        return matches

    def get_recipe_activity_stats(self, recipe_id: UUID) -> RecipeActivityStats:
        return self.stats.get(recipe_id, RecipeActivityStats()).model_copy(deep=True)

    def get_recipes_activity_stats(
        self, recipe_ids: list[UUID] | None = None
    ) -> dict[UUID, RecipeActivityStats]:
        target_ids = recipe_ids if recipe_ids is not None else list(self.recipes.keys())
        return {
            rid: self.stats.get(rid, RecipeActivityStats()).model_copy(deep=True)
            for rid in target_ids
        }

    def get_household_locale(self) -> str:
        return self.household_locale
