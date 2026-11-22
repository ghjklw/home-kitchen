"""Compatibility facade composing domain services for recipes, meal agenda, and shopping."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from home_kitchen.catalogue.adapters import CatalogueRecipeAutoMapperAdapter
from home_kitchen.catalogue.identity import UnmappedIngredient
from home_kitchen.catalogue.identity_repository import PostgresIdentityRepository
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.catalogue.unmapped_service import UnmappedIngredientService
from home_kitchen.models import (
    Meal,
    MealCreate,
    MealUpdate,
    PlannedMealRecipe,
    PrepCategory,
    Recipe,
    RecipeActivityStats,
    RecipeCreate,
    RecipeSource,
    RecipeTranslation,
    RecipeTranslationUpdate,
    RecipeType,
    RegularItem,
    ShoppingItem,
    ShoppingItemCreate,
    WeekShopping,
)
from home_kitchen.planning.application.agenda import MealAgendaService
from home_kitchen.recipes.adapters.postgres import PostgresHouseholdLocaleAdapter
from home_kitchen.recipes.service import (
    RecipeService,
    retain_omitted_ingredient_identity_fields,
)
from home_kitchen.recipes.translation import AIRecipeTranslator, RecipeTranslator
from home_kitchen.settings import Household, Settings
from home_kitchen.shopping import (
    ClosedSourceKey,
    ShoppingBuilder,
    ShoppingContext,
    ShoppingService,
    default_shopping_builder,
)
from home_kitchen.store import Store, resolve_recipe_mapping_repository


class _LegacyStoreAdapter:
    """Adapts a duck-typed or custom Store double to the domain repository Protocols."""

    def __init__(self, store: Any) -> None:
        self._store = store

    # RecipeRepository
    def save(self, recipe: Recipe) -> None:
        self._store.save("recipe", recipe)

    def get(self, recipe_id: UUID) -> Recipe:
        return self._store.get("recipe", recipe_id, Recipe)

    def list_all(self) -> list[Recipe]:
        return list(self._store.list("recipe", Recipe))

    def delete(self, recipe_id: UUID) -> None:
        self._store.delete("recipe", recipe_id)

    # SingleRecipeReader
    def get_recipe(self, recipe_id: UUID) -> Recipe:
        return self.get(recipe_id)

    # RecipeMealReferenceChecker
    def has_meals_for_recipe(self, recipe_id: UUID) -> bool:
        return any(meal.recipe_id == recipe_id for meal in self._store.list("meal", Meal))

    # HouseholdLocalePort
    def get_household_locale(self) -> str:
        try:
            value = self._store.get_setting("household")
            if value:
                return Household.model_validate_json(value).language
        except Exception:
            pass
        return "nb"

    # RecipeTranslationRepository
    def save_translation(self, translation: RecipeTranslation) -> None:
        self._store.save_recipe_translation(translation)

    def get_translation(
        self, recipe_id: UUID, target_locale: str, recipe_version: str | None = None
    ) -> RecipeTranslation | None:
        return self._store.get_recipe_translation(recipe_id, target_locale, recipe_version)

    def list_translations(self, recipe_id: UUID) -> list[RecipeTranslation]:
        return list(self._store.list_recipe_translations(recipe_id))

    # RecipeStatsReader
    def get_recipe_activity_stats(self, recipe_id: UUID) -> RecipeActivityStats:
        getter = getattr(self._store, "get_recipe_activity_stats", None)
        if callable(getter):
            return getter(recipe_id)
        return RecipeActivityStats()

    def get_recipes_activity_stats(
        self, recipe_ids: list[UUID] | None = None
    ) -> dict[UUID, RecipeActivityStats]:
        getter = getattr(self._store, "get_recipes_activity_stats", None)
        if callable(getter):
            return getter(recipe_ids)
        return {}

    # MealAgendaRepository
    def save_meal(self, meal: Meal) -> None:
        self._store.save("meal", meal)

    def get_meal(self, meal_id: UUID) -> Meal:
        return self._store.get("meal", meal_id, Meal)

    def update_meal(self, meal: Meal, *, expected_version: int) -> Meal:
        updated = self._store.update_meal(meal, expected_version=expected_version)
        return updated or self._store.get("meal", meal.id, Meal)

    def list_meals(self) -> list[Meal]:
        return list(self._store.list("meal", Meal))

    def delete_meal(self, meal_id: UUID) -> None:
        self._store.delete("meal", meal_id)

    # ShoppingItemRepository
    def save_item(self, item: ShoppingItem) -> None:
        self._store.save("item", item)

    def get_item(self, item_id: UUID) -> ShoppingItem:
        return self._store.get("item", item_id, ShoppingItem)

    def list_items(self) -> list[ShoppingItem]:
        return list(self._store.list("item", ShoppingItem))

    def delete_item(self, item_id: UUID) -> None:
        self._store.delete("item", item_id)

    def save_regular(self, item: RegularItem) -> None:
        self._store.save("regular", item)

    def get_regular(self, item_id: UUID) -> RegularItem:
        return self._store.get("regular", item_id, RegularItem)

    def list_regulars(self) -> list[RegularItem]:
        return list(self._store.list("regular", RegularItem))

    def active_confirmations(self) -> set[UUID]:
        return set(self._store.active_confirmations())

    def confirm_regular(self, regular_id: UUID, item: ShoppingItem) -> ShoppingItem:
        return self._store.confirm(regular_id, item)


class KitchenService:
    """Backwards-compatible facade over Recipe, MealAgenda, Shopping, and Catalogue services."""

    _retain_omitted_ingredient_identity_fields = staticmethod(
        retain_omitted_ingredient_identity_fields
    )

    def __init__(
        self,
        database_url: str = "",
        shopping_builder: ShoppingBuilder | None = None,
        identity_service: CanonicalIdentityService | None = None,
        store: Any = None,
        translator: RecipeTranslator | None = None,
        settings: Settings | None = None,
        household_id: UUID | None = None,
        *,
        recipe_service: RecipeService | None = None,
        meal_agenda: MealAgendaService | None = None,
        shopping_service: ShoppingService | None = None,
        unmapped_service: UnmappedIngredientService | None = None,
    ):
        self.store: Any = store if store is not None else Store(database_url)
        self.shopping_builder = shopping_builder or default_shopping_builder()
        self.identities = identity_service or CanonicalIdentityService(
            PostgresIdentityRepository(self.store.engine)
        )
        self._translator = translator
        self._settings = settings
        self._household_id = household_id or (settings.entra_allowed_group if settings else None)

        if isinstance(self.store, Store):
            recipe_adapter: Any = self.store._recipes_repo()
            meal_adapter: Any = self.store._meals_repo()
            shopping_adapter: Any = self.store._shopping_repo()
            locale_reader: Any = PostgresHouseholdLocaleAdapter(self.store.engine)
        else:
            legacy = _LegacyStoreAdapter(self.store)
            recipe_adapter = legacy
            meal_adapter = legacy
            shopping_adapter = legacy
            locale_reader = legacy

        auto_mapper = CatalogueRecipeAutoMapperAdapter(
            resolve_recipe_mapping_repository(self.store),
            settings_provider=lambda: self._settings,
            household_id_provider=lambda: self._household_id,
            identity_service=self.identities,
        )

        def _default_translator() -> RecipeTranslator | None:
            if self._settings is not None and self._settings.ai_configured:
                return AIRecipeTranslator(self._settings)
            return None

        self.recipe_service = recipe_service or RecipeService(
            recipes=recipe_adapter,
            identities=self.identities,
            translations=recipe_adapter,
            stats=recipe_adapter,
            meal_checker=recipe_adapter,
            locale_reader=locale_reader,
            auto_mapper=auto_mapper,
            translator=translator,
            default_translator_factory=_default_translator,
            household_id=self._household_id,
        )
        self.meal_agenda = meal_agenda or MealAgendaService(
            meals=meal_adapter,
            recipes=self.recipe_service,
        )
        self.shopping_service = shopping_service or ShoppingService(
            items=shopping_adapter,
            meals=self.meal_agenda,
            recipes=self.recipe_service,
            shopping_builder=self.shopping_builder,
        )
        self.unmapped_service = unmapped_service or UnmappedIngredientService(
            recipes=self.recipe_service,
            identities=self.identities,
            household_id=self._household_id,
        )

    @property
    def translator(self) -> RecipeTranslator | None:
        return self._translator

    @translator.setter
    def translator(self, value: RecipeTranslator | None) -> None:
        self._translator = value
        self.recipe_service.translator = value

    @property
    def settings(self) -> Settings | None:
        return self._settings

    @settings.setter
    def settings(self, value: Settings | None) -> None:
        self._settings = value

    @property
    def household_id(self) -> UUID | None:
        return self._household_id

    @household_id.setter
    def household_id(self, value: UUID | None) -> None:
        self._household_id = value
        self.recipe_service.household_id = value
        self.unmapped_service.household_id = value

    # ---- Recipes & Translations ----

    def create_recipe(self, data: RecipeCreate) -> Recipe:
        return self.recipe_service.create_recipe(data)

    def list_recipes(
        self,
        recipe_type: RecipeType | str | None = None,
        prep_category: PrepCategory | str | None = None,
        max_prep_minutes: int | None = None,
        include_stats: bool = False,
        source: RecipeSource | str | None = None,
    ) -> list[Recipe]:
        return self.recipe_service.list_recipes(
            recipe_type=recipe_type,
            prep_category=prep_category,
            max_prep_minutes=max_prep_minutes,
            include_stats=include_stats,
            source=source,
        )

    def get_recipe(self, recipe_id: UUID, include_stats: bool = False) -> Recipe:
        return self.recipe_service.get_recipe(recipe_id, include_stats=include_stats)

    def get_recipe_stats(self, recipe_id: UUID) -> RecipeActivityStats:
        return self.recipe_service.get_recipe_stats(recipe_id)

    def get_recipes_stats(
        self, recipe_ids: list[UUID] | None = None
    ) -> dict[UUID, RecipeActivityStats]:
        return self.recipe_service.get_recipes_stats(recipe_ids)

    def update_recipe(self, recipe_id: UUID, data: RecipeCreate) -> Recipe:
        return self.recipe_service.update_recipe(recipe_id, data)

    def resolve_recipe_identities(self, recipe: Recipe) -> Recipe:
        return self.recipe_service.resolve_recipe_identities(recipe)

    def delete_recipe(self, recipe_id: UUID) -> None:
        self.recipe_service.delete_recipe(recipe_id)

    def get_recipe_translation(
        self, recipe_id: UUID, target_locale: str
    ) -> RecipeTranslation | None:
        return self.recipe_service.get_recipe_translation(recipe_id, target_locale)

    def list_recipe_translations(self, recipe_id: UUID) -> list[RecipeTranslation]:
        return self.recipe_service.list_recipe_translations(recipe_id)

    async def translate_recipe(
        self, recipe_id: UUID, target_locale: str, force: bool = False
    ) -> RecipeTranslation:
        return await self.recipe_service.translate_recipe(recipe_id, target_locale, force=force)

    def update_recipe_translation(
        self, recipe_id: UUID, target_locale: str, data: RecipeTranslationUpdate
    ) -> RecipeTranslation:
        return self.recipe_service.update_recipe_translation(recipe_id, target_locale, data)

    # ---- Meal Agenda ----

    def plan_meal(self, data: MealCreate) -> Meal:
        return self.meal_agenda.plan_meal(data)

    def update_meal(self, meal_id: UUID, data: MealUpdate) -> Meal:
        return self.meal_agenda.update_meal(meal_id, data)

    def get_meal(self, meal_id: UUID) -> Meal:
        return self.meal_agenda.get_meal(meal_id)

    def list_meals(self, start: date, end: date) -> list[Meal]:
        return self.meal_agenda.list_meals(start, end)

    def meal_recipe(self, meal_id: UUID) -> PlannedMealRecipe:
        return self.meal_agenda.meal_recipe(meal_id)

    def delete_meal(self, meal_id: UUID) -> None:
        self.meal_agenda.delete_meal(meal_id)

    # ---- Shopping Captures & Regular Items ----

    def add_item(self, data: ShoppingItemCreate) -> ShoppingItem:
        return self.shopping_service.add_item(data)

    def delete_item(self, item_id: UUID) -> None:
        self.shopping_service.delete_item(item_id)

    def list_items(self) -> list[ShoppingItem]:
        return self.shopping_service.list_items()

    def complete_item(self, item_id: UUID, completed: bool) -> ShoppingItem:
        return self.shopping_service.complete_item(item_id, completed)

    def add_regular_item(self, data: ShoppingItemCreate, interval_days: int = 7) -> RegularItem:
        return self.shopping_service.add_regular_item(data, interval_days)

    def list_regular_items(self) -> list[RegularItem]:
        return self.shopping_service.list_regular_items()

    def record_purchase(self, item_id: UUID, purchased_on: date) -> RegularItem:
        return self.shopping_service.record_purchase(item_id, purchased_on)

    def confirm_suggestion(self, item_id: UUID) -> ShoppingItem:
        return self.shopping_service.confirm_suggestion(item_id)

    def shopping_list(
        self,
        start: date,
        end: date,
        today: date,
        *,
        closed_sources: set[ClosedSourceKey] | None = None,
    ) -> WeekShopping:
        if hasattr(self, "shopping_service"):
            return self.shopping_service.shopping_list(
                start, end, today, closed_sources=closed_sources
            )
        if end < start:
            raise ValueError("end must be on or after start")
        context = ShoppingContext(
            meals=self.list_meals(start, end),
            recipes={recipe.id: recipe for recipe in self.list_recipes()},
            captures=self.list_items(),
            regulars=self.list_regular_items(),
            active_confirmations=self.store.active_confirmations(),
            today=today,
            start=start,
            end=end,
            closed_sources=closed_sources or set(),
        )
        return self.shopping_builder.build(context)

    # ---- Unmapped Ingredients ----

    def list_unmapped_ingredients(self, recipe_id: UUID | None = None) -> list[UnmappedIngredient]:
        return self.unmapped_service.list_unmapped_ingredients(recipe_id=recipe_id)

    def map_unmapped_ingredient(
        self,
        recipe_id: UUID,
        ingredient_index: int,
        canonical_product_id: UUID | None = None,
        canonical_unit_id: UUID | None = None,
        *,
        add_alias: bool = False,
        locale: str = "nb",
        context: str = "recipe",
    ) -> Recipe:
        return self.unmapped_service.map_unmapped_ingredient(
            recipe_id=recipe_id,
            ingredient_index=ingredient_index,
            canonical_product_id=canonical_product_id,
            canonical_unit_id=canonical_unit_id,
            add_alias=add_alias,
            locale=locale,
            context=context,
        )
