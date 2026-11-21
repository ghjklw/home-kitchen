"""Household recipes, meal planning, and deliberate shopping suggestions."""

import logging
from collections import Counter
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID, uuid4

from home_kitchen.catalogue.identity import (
    UnmappedIngredient,
    candidate_identity_names,
    normalize_identity_text,
)
from home_kitchen.catalogue.identity_repository import PostgresIdentityRepository
from home_kitchen.catalogue.identity_service import CanonicalIdentityService

from .context import history_context
from .models import (
    Meal,
    MealCreate,
    PlannedMealRecipe,
    PrepCategory,
    PurchaseEstimate,
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
    normalize_prep_category_value,
    normalize_recipe_source_value,
    normalize_recipe_type_value,
)
from .recipes.language import detect_recipe_language
from .recipes.source_quantities import normalize_source_quantity, source_ingredient_parts
from .recipes.translation import RecipeTranslator, compute_recipe_version
from .settings import Settings
from .shopping import ClosedSourceKey, ShoppingBuilder, ShoppingContext, default_shopping_builder
from .store import Store
from .units import scale_quantity

_LOG = logging.getLogger(__name__)
_PURCHASABLE_SOURCE_UNITS = frozenset({"g", "kg", "mg", "ml", "cl", "dl", "l", "stk", "item"})
_LOCALE_NORMALIZATION = {
    "nob": "nb",
    "nor": "nb",
    "no": "nb",
    "eng": "en",
    "fra": "fr",
    "fre": "fr",
    "dan": "da",
    "swe": "sv",
}


class KitchenService:
    def __init__(
        self,
        database_url: str = "",
        shopping_builder: ShoppingBuilder | None = None,
        identity_service: CanonicalIdentityService | None = None,
        store: Store | None = None,
        translator: RecipeTranslator | None = None,
        settings: Settings | None = None,
        household_id: UUID | None = None,
    ):
        if store is not None:
            self.store = store
        else:
            self.store = Store(database_url)
        self.shopping_builder = shopping_builder or default_shopping_builder()
        self.identities = identity_service or CanonicalIdentityService(
            PostgresIdentityRepository(self.store.engine)
        )
        self.translator = translator
        self.settings = settings
        self.household_id = household_id or (settings.entra_allowed_group if settings else None)

    def create_recipe(self, data: RecipeCreate) -> Recipe:
        resolved_data = self._resolve_recipe_identities_on_create(data)
        self._validate_canonical_identities(resolved_data)
        source_language = data.source_language
        if not source_language:
            source_language = detect_recipe_language(resolved_data)
        # Image credit is written only by the authenticated candidate-selection flow.
        recipe = Recipe(
            id=uuid4(),
            source_language=source_language,
            **self._recipe_values(resolved_data, exclude={"image_attribution", "source_language"}),
        )
        self.store.save("recipe", recipe)
        return self._auto_map_recipe_if_needed(recipe)

    def list_recipes(
        self,
        recipe_type: RecipeType | str | None = None,
        prep_category: PrepCategory | str | None = None,
        max_prep_minutes: int | None = None,
        include_stats: bool = False,
        source: RecipeSource | str | None = None,
    ) -> list[Recipe]:
        recipes = self.store.list("recipe", Recipe)
        normalized_type = normalize_recipe_type_value(recipe_type)
        if normalized_type is not None:
            target_type = str(normalized_type).strip().casefold()
            recipes = [
                r
                for r in recipes
                if r.recipe_type is not None
                and str(r.recipe_type).strip().casefold() == target_type
            ]
        normalized_category = normalize_prep_category_value(prep_category)
        if normalized_category is not None:
            target_category = str(normalized_category).strip().casefold()
            recipes = [
                r
                for r in recipes
                if r.prep_category is not None
                and str(r.prep_category).strip().casefold() == target_category
            ]
        if max_prep_minutes is not None:
            recipes = [
                r
                for r in recipes
                if r.prep_time_minutes is not None and r.prep_time_minutes <= max_prep_minutes
            ]
        normalized_source = normalize_recipe_source_value(source)
        if normalized_source is not None:
            target_source = str(normalized_source).strip().casefold()
            recipes = [
                r
                for r in recipes
                if r.source is not None and str(r.source).strip().casefold() == target_source
            ]
        if include_stats:
            stats_map = self.store.get_recipes_activity_stats([r.id for r in recipes])
            for recipe in recipes:
                stats = stats_map.get(recipe.id, RecipeActivityStats())
                recipe.activity_stats = stats
                recipe.stats = stats
        return recipes

    def get_recipe(self, recipe_id: UUID, include_stats: bool = False) -> Recipe:
        recipe = self.store.get("recipe", recipe_id, Recipe)
        if include_stats:
            stats = self.store.get_recipe_activity_stats(recipe_id)
            recipe.activity_stats = stats
            recipe.stats = stats
        return recipe

    def get_recipe_stats(self, recipe_id: UUID) -> RecipeActivityStats:
        self.get_recipe(recipe_id)
        return self.store.get_recipe_activity_stats(recipe_id)

    def get_recipes_stats(
        self, recipe_ids: list[UUID] | None = None
    ) -> dict[UUID, RecipeActivityStats]:
        return self.store.get_recipes_activity_stats(recipe_ids)

    def update_recipe(self, recipe_id: UUID, data: RecipeCreate) -> Recipe:
        existing = self.get_recipe(recipe_id)
        self._validate_canonical_identities(data)
        source_language = data.source_language
        if not source_language:
            detected = detect_recipe_language(data)
            if detected != "und" or not existing.source_language:
                source_language = detected
            else:
                source_language = existing.source_language
        values = self._recipe_values(data, exclude={"image_attribution", "source_language"})
        if "source" not in data.model_fields_set:
            values["source"] = existing.source
        if "source_url" not in data.model_fields_set and "provenance" not in data.model_fields_set:
            values["source_url"] = existing.source_url
            values["provenance"] = (
                existing.provenance.model_dump() if existing.provenance is not None else None
            )
        elif "source_url" not in data.model_fields_set and data.provenance is None:
            values["source_url"] = existing.source_url
        if "photo_source" not in data.model_fields_set:
            values["photo_source"] = (
                existing.photo_source.model_dump() if existing.photo_source is not None else None
            )
        if "guided_steps" not in data.model_fields_set:
            values["guided_steps"] = [step.model_dump() for step in existing.guided_steps]
        values["ingredients"] = self._retain_omitted_ingredient_identity_fields(
            data, existing, values["ingredients"]
        )
        values["ingredients"], excluded_fields = self._resolve_ingredient_identities_on_update(
            data,
            existing,
            values["ingredients"],
            recipe_id=recipe_id,
            recipe_locale=source_language,
        )
        self._validate_ingredient_dict_identities(values["ingredients"])
        values["image_attribution"] = (
            existing.image_attribution if data.image_url == existing.image_url else None
        )
        recipe = Recipe(id=recipe_id, source_language=source_language, **values)
        self.store.save("recipe", recipe)
        return self._auto_map_recipe_if_needed(recipe, excluded_fields=excluded_fields)

    @staticmethod
    def _retain_omitted_ingredient_identity_fields(
        data: RecipeCreate, existing: Recipe, current_values: list[dict]
    ) -> list[dict]:
        """Preserve omitted metadata only for a unique, unchanged ingredient row."""
        preserved = [dict(values) for values in current_values]
        fields = ("name", "quantity", "unit", "category", "source_indices", "purchase_estimate")
        source_fields = ("name", "unit", "category", "source_indices")

        def freeze(value):
            if isinstance(value, dict):
                return tuple((key, freeze(item)) for key, item in sorted(value.items()))
            if isinstance(value, list):
                return tuple(freeze(item) for item in value)
            return value

        def signature(values: dict) -> tuple:
            return tuple(freeze(values.get(field)) for field in fields)

        def source_signature(values: dict) -> tuple:
            return tuple(freeze(values.get(field)) for field in source_fields)

        existing_values = [item.model_dump() for item in existing.ingredients]
        existing_signatures = Counter(signature(item) for item in existing_values)
        incoming_signatures = Counter(signature(item) for item in preserved)
        existing_source_signatures = Counter(source_signature(item) for item in existing_values)
        incoming_source_signatures = Counter(source_signature(item) for item in preserved)
        for index, ingredient in enumerate(data.ingredients):
            if index >= len(existing.ingredients):
                continue

            values = preserved[index]
            prior = existing_values[index]
            prior_has_identity = bool(
                prior.get("canonical_product_id") or prior.get("canonical_unit_id")
            )
            omitted_identity_fields = {
                field
                for field in ("canonical_product_id", "canonical_unit_id")
                if field not in ingredient.model_fields_set
            }
            current_signature = signature(values)
            unchanged_and_unique = (
                current_signature == signature(prior)
                and existing_signatures[current_signature] == 1
                and incoming_signatures[current_signature] == 1
            )
            if prior_has_identity and omitted_identity_fields and not unchanged_and_unique:
                raise ValueError(
                    "Recipe update must include identity metadata for changed or ambiguous ingredients"
                )
            if unchanged_and_unique:
                for field in omitted_identity_fields:
                    values[field] = prior.get(field)
            source_key = source_signature(values)
            source_expression_unchanged = unchanged_and_unique or (
                source_key == source_signature(prior)
                and existing_source_signatures[source_key] == 1
                and incoming_source_signatures[source_key] == 1
            )
            if (
                source_expression_unchanged
                and "source_unit_expression" not in ingredient.model_fields_set
            ):
                values["source_unit_expression"] = prior.get("source_unit_expression")
        return preserved

    def _household_locale(self) -> str:
        try:
            value = self.store.get_setting("household")
            if value:
                from home_kitchen.settings import Household

                return Household.model_validate_json(value).language
        except Exception:
            pass
        return "nb"

    def _resolution_locales(self, recipe_locale: str | None = None) -> list[str]:
        candidates = [self._household_locale(), recipe_locale or "", "nb", "en", "fr"]
        locales: list[str] = []
        for raw in candidates:
            cleaned = normalize_identity_text(raw)
            if not cleaned or cleaned == "und":
                continue
            normalized = _LOCALE_NORMALIZATION.get(cleaned, cleaned)
            if normalized not in locales:
                locales.append(normalized)
        return locales

    def _resolve_exact_identity_across_locales(
        self, kind: str, alias: str, locales: list[str]
    ) -> tuple[UUID | None, bool]:
        for locale in locales:
            try:
                res = self.identities.resolve(
                    kind, locale, "recipe", alias, household_id=self.household_id
                )
            except TypeError:
                res = self.identities.resolve(kind, locale, "recipe", alias)
            if res.status == "resolved" and len(res.candidates) == 1:
                return res.candidates[0].id, False
            if res.status == "ambiguous":
                return None, True
        return None, False

    def _resolve_identity_across_locales(
        self, kind: str, alias: str, locales: list[str]
    ) -> UUID | None:
        exact_id, ambiguous = self._resolve_exact_identity_across_locales(kind, alias, locales)
        if exact_id is not None or ambiguous or kind != "product":
            return exact_id
        norm_alias = normalize_identity_text(alias)
        for candidate in candidate_identity_names(alias):
            if candidate == norm_alias:
                continue
            matched_id, cand_ambiguous = self._resolve_exact_identity_across_locales(
                kind, candidate, locales
            )
            if matched_id is not None:
                return matched_id
            if cand_ambiguous:
                return None
        return None

    def resolve_recipe_identities(self, recipe: Recipe) -> Recipe:
        """Apply fast exact alias resolution across household and recipe locales."""
        source_lang = getattr(recipe, "source_language", None)
        if not source_lang or source_lang == "und":
            source_lang = detect_recipe_language(recipe)
        locales = self._resolution_locales(source_lang)
        new_ingredients = []
        changed = False
        for ingredient in recipe.ingredients:
            updates: dict[str, Any] = {}
            if ingredient.canonical_product_id is None and ingredient.name:
                prod_id = self._resolve_identity_across_locales("product", ingredient.name, locales)
                if prod_id is not None:
                    updates["canonical_product_id"] = prod_id
            if ingredient.canonical_unit_id is None and ingredient.unit:
                unit_id = self._resolve_identity_across_locales("unit", ingredient.unit, locales)
                if unit_id is not None:
                    updates["canonical_unit_id"] = unit_id
            if updates:
                changed = True
                new_ingredients.append(ingredient.model_copy(update=updates))
            else:
                new_ingredients.append(ingredient)
        if changed:
            return recipe.model_copy(update={"ingredients": new_ingredients})
        return recipe

    def _auto_map_recipe_if_needed(
        self,
        recipe: Recipe,
        *,
        excluded_fields: dict[tuple[UUID, int], set[str]] | None = None,
    ) -> Recipe:
        settings = self.settings
        if settings is None or not settings.ai_configured:
            return recipe
        has_unmapped = False
        for index, ingredient in enumerate(recipe.ingredients):
            excluded = (excluded_fields or {}).get((recipe.id, index), set())
            needs_product = "product" not in excluded and ingredient.canonical_product_id is None
            needs_unit = (
                "unit" not in excluded
                and bool(ingredient.unit and ingredient.unit.strip())
                and ingredient.canonical_unit_id is None
            )
            if needs_product or needs_unit:
                has_unmapped = True
                break
        if not has_unmapped:
            return recipe
        try:
            from home_kitchen.catalogue.ai_mapper import auto_map_unmapped_ingredients

            summary = auto_map_unmapped_ingredients(
                self.store,
                settings,
                household_id=self.household_id,
                actor_id=history_context.get().actor_id or "system",
                recipe_id=recipe.id,
                identity_service=self.identities,
                excluded_fields=excluded_fields,
            )
            if summary.get("mapped_occurrences", 0) > 0:
                return self.store.get("recipe", recipe.id, Recipe)
        except Exception:
            _LOG.warning(
                "Auto-mapping unmapped ingredients failed for recipe %s",
                recipe.id,
                exc_info=True,
            )
        return recipe

    def _resolve_recipe_identities_on_create(self, data: RecipeCreate) -> RecipeCreate:
        recipe_locale = data.source_language or detect_recipe_language(data)
        locales = self._resolution_locales(recipe_locale)
        new_ingredients = []
        changed = False
        for ingredient in data.ingredients:
            updates: dict[str, Any] = {}
            if ingredient.canonical_product_id is None and ingredient.name:
                prod_id = self._resolve_identity_across_locales("product", ingredient.name, locales)
                if prod_id is not None:
                    updates["canonical_product_id"] = prod_id
            if ingredient.canonical_unit_id is None and ingredient.unit:
                unit_id = self._resolve_identity_across_locales("unit", ingredient.unit, locales)
                if unit_id is not None:
                    updates["canonical_unit_id"] = unit_id
            if updates:
                changed = True
                new_ingredients.append(ingredient.model_copy(update=updates))
            else:
                new_ingredients.append(ingredient)
        if changed:
            return data.model_copy(update={"ingredients": new_ingredients})
        return data

    def _resolve_ingredient_identities_on_update(
        self,
        data: RecipeCreate,
        existing: Recipe,
        current_values: list[dict[str, Any]],
        *,
        recipe_id: UUID | None = None,
        recipe_locale: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[tuple[UUID, int], set[str]]]:
        locales = self._resolution_locales(recipe_locale)
        resolved = [dict(v) for v in current_values]
        excluded_fields: dict[tuple[UUID, int], set[str]] = {}
        target_recipe_id = recipe_id or existing.id
        for index, item in enumerate(resolved):
            incoming = data.ingredients[index] if index < len(data.ingredients) else None
            prior = existing.ingredients[index] if index < len(existing.ingredients) else None

            product_cleared = False
            if incoming is not None and prior is not None:
                if (
                    prior.canonical_product_id is not None
                    and incoming.canonical_product_id is None
                    and "canonical_product_id" in incoming.model_fields_set
                    and normalize_identity_text(incoming.name)
                    == normalize_identity_text(prior.name)
                ):
                    product_cleared = True
                    excluded_fields.setdefault((target_recipe_id, index), set()).add("product")

            unit_cleared = False
            if incoming is not None and prior is not None:
                if (
                    prior.canonical_unit_id is not None
                    and incoming.canonical_unit_id is None
                    and "canonical_unit_id" in incoming.model_fields_set
                    and normalize_identity_text(incoming.unit or "")
                    == normalize_identity_text(prior.unit or "")
                ):
                    unit_cleared = True
                    excluded_fields.setdefault((target_recipe_id, index), set()).add("unit")

            if (
                item.get("canonical_product_id") is None
                and not product_cleared
                and item.get("name")
            ):
                prod_id = self._resolve_identity_across_locales("product", item["name"], locales)
                if prod_id is not None:
                    item["canonical_product_id"] = prod_id

            if item.get("canonical_unit_id") is None and not unit_cleared and item.get("unit"):
                unit_id = self._resolve_identity_across_locales("unit", item["unit"], locales)
                if unit_id is not None:
                    item["canonical_unit_id"] = unit_id

        return resolved, excluded_fields

    def _validate_canonical_identities(self, data: RecipeCreate) -> None:
        product_ids = {
            item.canonical_product_id for item in data.ingredients if item.canonical_product_id
        }
        unit_ids = {item.canonical_unit_id for item in data.ingredients if item.canonical_unit_id}
        self.identities.validate_identities("product", product_ids)
        self.identities.validate_identities("unit", unit_ids)

    def _validate_ingredient_dict_identities(self, ingredients: list[dict[str, Any]]) -> None:
        product_ids = {
            item["canonical_product_id"] for item in ingredients if item.get("canonical_product_id")
        }
        unit_ids = {
            item["canonical_unit_id"] for item in ingredients if item.get("canonical_unit_id")
        }
        self.identities.validate_identities("product", product_ids)
        self.identities.validate_identities("unit", unit_ids)

    @staticmethod
    def _recipe_values(data: RecipeCreate, *, exclude: set[str]) -> dict:
        """Derive purchase estimates only from explicitly linked source rows."""
        values = data.model_dump(exclude=exclude)
        source_rows = {row.index: row.text for row in data.source_ingredients}
        ingredients = []
        for ingredient in data.ingredients:
            item = ingredient.model_dump()
            estimate = None
            if len(ingredient.source_indices) == 1:
                source_text = source_rows.get(ingredient.source_indices[0])
                if source_text is not None:
                    source_name, parsed_source = source_ingredient_parts(source_text)
                    normalized_name = " ".join(ingredient.name.casefold().split())
                    linked_name = " ".join(source_name.casefold().split())
                    if normalized_name == linked_name and ingredient.unit not in {
                        "klype",
                        "håndfull",
                        "etter smak",
                    }:
                        quantity = ingredient.quantity or parsed_source.quantity
                        unit = ingredient.unit or parsed_source.unit
                        if quantity is not None and unit is not None:
                            parsed_current = normalize_source_quantity(
                                f"{quantity} {unit} {ingredient.name}", ingredient.name
                            )
                            if unit in {"klype", "håndfull", "etter smak"}:
                                parsed_current = None
                            if parsed_current is not None:
                                if parsed_current.purchase_estimate is not None:
                                    values_for_estimate = parsed_current.purchase_estimate.__dict__
                                elif unit in _PURCHASABLE_SOURCE_UNITS:
                                    values_for_estimate = {
                                        "quantity": quantity,
                                        "unit": unit,
                                        "approximate": False,
                                        "provenance": "Current recipe measure",
                                        "source_url": None,
                                    }
                                else:
                                    values_for_estimate = None
                                if values_for_estimate is not None:
                                    values_for_estimate = dict(values_for_estimate)
                                    values_for_estimate["provenance"] = (
                                        f"{values_for_estimate['provenance']}; "
                                        f"original source text: {source_text}"
                                    )
                                    estimate = PurchaseEstimate.model_validate(
                                        values_for_estimate
                                    ).model_dump()
            # Treat estimates as derived data: never accept an unsupported amount
            # supplied by a client or model, and keep the recipe's cooking measure.
            item["purchase_estimate"] = estimate
            ingredients.append(item)
        values["ingredients"] = ingredients
        return values

    def delete_recipe(self, recipe_id: UUID) -> None:
        self.get_recipe(recipe_id)
        if any(meal.recipe_id == recipe_id for meal in self.store.list("meal", Meal)):
            raise ValueError("Remove planned meals before deleting this recipe")
        self.store.delete("recipe", recipe_id)

    def get_recipe_translation(
        self, recipe_id: UUID, target_locale: str
    ) -> RecipeTranslation | None:
        recipe = self.get_recipe(recipe_id)
        current_version = compute_recipe_version(recipe)
        translation = self.store.get_recipe_translation(recipe_id, target_locale.strip().casefold())
        if translation is None:
            return None
        is_stale = translation.recipe_version != current_version
        return translation.model_copy(update={"is_stale": is_stale})

    def list_recipe_translations(self, recipe_id: UUID) -> list[RecipeTranslation]:
        recipe = self.get_recipe(recipe_id)
        current_version = compute_recipe_version(recipe)
        translations = self.store.list_recipe_translations(recipe_id)
        return [
            t.model_copy(update={"is_stale": t.recipe_version != current_version})
            for t in translations
        ]

    async def translate_recipe(
        self, recipe_id: UUID, target_locale: str, force: bool = False
    ) -> RecipeTranslation:
        recipe = self.get_recipe(recipe_id)
        target_locale = target_locale.strip().casefold()
        current_version = compute_recipe_version(recipe)

        if not force:
            existing = self.store.get_recipe_translation(recipe_id, target_locale, current_version)
            if existing is not None:
                return existing.model_copy(update={"is_stale": False})

        # Identity translation if recipe is already in the requested target language
        if recipe.source_language and recipe.source_language.casefold() == target_locale:
            translation = RecipeTranslation(
                id=uuid4(),
                recipe_id=recipe_id,
                target_locale=target_locale,
                recipe_version=current_version,
                title=recipe.title,
                instructions=recipe.instructions,
                ingredients=recipe.ingredients,
                provider="source",
                model="source-language",
                created_at=datetime.now(UTC),
                is_stale=False,
            )
            self.store.save_recipe_translation(translation)
            return translation

        translator = self.translator
        if translator is None:
            if self.settings and self.settings.ai_configured:
                from home_kitchen.recipes.translation import AIRecipeTranslator

                translator = AIRecipeTranslator(self.settings)
            else:
                raise ValueError("Configure an AI model and API credential to translate recipes")

        payload = await translator.translate(recipe, target_locale)
        translation = RecipeTranslation(
            id=uuid4(),
            recipe_id=recipe_id,
            target_locale=target_locale,
            recipe_version=current_version,
            title=payload.title,
            instructions=payload.instructions,
            ingredients=payload.ingredients,
            provider=payload.provider,
            model=payload.model,
            created_at=datetime.now(UTC),
            is_stale=False,
        )
        self.store.save_recipe_translation(translation)
        return translation

    def update_recipe_translation(
        self, recipe_id: UUID, target_locale: str, data: RecipeTranslationUpdate
    ) -> RecipeTranslation:
        """Save a manual edit to a recipe translation without modifying the original recipe."""
        recipe = self.get_recipe(recipe_id)
        normalized_locale = target_locale.strip().casefold()
        if not normalized_locale:
            raise ValueError("Target locale must not be blank")
        current_version = compute_recipe_version(recipe)
        existing = self.store.get_recipe_translation(recipe_id, normalized_locale, current_version)
        translation = RecipeTranslation(
            id=existing.id if existing is not None else uuid4(),
            recipe_id=recipe_id,
            target_locale=normalized_locale,
            recipe_version=current_version,
            title=data.title,
            instructions=data.instructions,
            ingredients=data.ingredients,
            provider="manual",
            model="manual",
            created_at=datetime.now(UTC),
            is_stale=False,
        )
        self.store.save_recipe_translation(translation)
        return translation

    def plan_meal(self, data: MealCreate) -> Meal:
        self.get_recipe(data.recipe_id)
        meal = Meal(id=uuid4(), **data.model_dump())
        self.store.save("meal", meal)
        return meal

    def update_meal(self, meal_id: UUID, data: MealCreate) -> Meal:
        self.get_recipe(data.recipe_id)
        self.store.get("meal", meal_id, Meal)
        meal = Meal(id=meal_id, **data.model_dump())
        self.store.update_meal(meal)
        return meal

    def list_meals(self, start: date, end: date) -> list[Meal]:
        self._validate_range(start, end)
        return sorted(
            (meal for meal in self.store.list("meal", Meal) if start <= meal.date <= end),
            # Store.list is ordered by the persistent insertion position. The stable sort
            # keeps that agenda order for untimed entries and uses explicit times where set.
            key=lambda meal: (meal.date, meal.scheduled_time or time.max),
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

    def shopping_list(
        self,
        start: date,
        end: date,
        today: date,
        *,
        closed_sources: set[ClosedSourceKey] | None = None,
    ) -> WeekShopping:
        self._validate_range(start, end)
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

    @staticmethod
    def _validate_range(start: date, end: date) -> None:
        if end < start:
            raise ValueError("end must be on or after start")

    def list_unmapped_ingredients(self, recipe_id: UUID | None = None) -> list[UnmappedIngredient]:
        recipes = [self.get_recipe(recipe_id)] if recipe_id else self.list_recipes()
        unmapped = []
        for recipe in recipes:
            for index, ingredient in enumerate(recipe.ingredients):
                needs_product = ingredient.canonical_product_id is None
                needs_unit = bool(ingredient.unit) and ingredient.canonical_unit_id is None
                if needs_product or needs_unit:
                    unmapped.append(
                        UnmappedIngredient(
                            recipe_id=recipe.id,
                            recipe_title=recipe.title,
                            ingredient_index=index,
                            name=ingredient.name,
                            quantity=ingredient.quantity,
                            unit=ingredient.unit,
                            source_unit_expression=ingredient.source_unit_expression,
                            canonical_product_id=ingredient.canonical_product_id,
                            canonical_unit_id=ingredient.canonical_unit_id,
                        )
                    )
        return unmapped

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
        recipe = self.get_recipe(recipe_id)
        if ingredient_index < 0 or ingredient_index >= len(recipe.ingredients):
            raise IndexError("Ingredient index out of range")
        if canonical_product_id is None and canonical_unit_id is None:
            raise ValueError("Must specify canonical_product_id or canonical_unit_id to map")

        target = recipe.ingredients[ingredient_index]
        if canonical_product_id is not None:
            self.identities.validate_identities("product", {canonical_product_id})
            if add_alias:
                try:
                    self.identities.add_alias(
                        "product",
                        locale,
                        context,
                        target.name,
                        canonical_product_id,
                        household_id=self.household_id,
                        source_type="reviewed_correction",
                    )
                except TypeError:
                    self.identities.add_alias(
                        "product", locale, context, target.name, canonical_product_id
                    )
        if canonical_unit_id is not None:
            self.identities.validate_identities("unit", {canonical_unit_id})
            if add_alias and target.unit:
                try:
                    self.identities.add_alias(
                        "unit",
                        locale,
                        context,
                        target.unit,
                        canonical_unit_id,
                        household_id=self.household_id,
                        source_type="reviewed_correction",
                    )
                except TypeError:
                    self.identities.add_alias(
                        "unit", locale, context, target.unit, canonical_unit_id
                    )

        updated_ingredients = []
        for idx, ing in enumerate(recipe.ingredients):
            if idx == ingredient_index:
                updates: dict[str, Any] = {}
                if canonical_product_id is not None:
                    updates["canonical_product_id"] = canonical_product_id
                if canonical_unit_id is not None:
                    updates["canonical_unit_id"] = canonical_unit_id
                updated_ingredients.append(ing.model_copy(update=updates))
            else:
                updated_ingredients.append(ing)

        recipe_create = RecipeCreate(
            **recipe.model_dump(exclude={"id", "ingredients"}),
            ingredients=updated_ingredients,
        )
        return self.update_recipe(recipe_id, recipe_create)
