"""Pure unit tests and AST import-boundary checks for Step 5 domain/port decomposition."""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from home_kitchen.bootstrap import create_in_memory_shopping_service
from home_kitchen.catalogue.identity import (
    AliasResolution,
    CanonicalProduct,
    CanonicalUnit,
)
from home_kitchen.catalogue.unmapped_service import UnmappedIngredientService
from home_kitchen.history import HistoryEntry, HistoryEntryCreate
from home_kitchen.models import (
    Ingredient,
    MealCreate,
    PrepCategory,
    Recipe,
    RecipeActivityStats,
    RecipeCreate,
    RecipeSource,
    RecipeTranslationUpdate,
    RecipeType,
    ShoppingItemCreate,
    SourceIngredient,
)
from home_kitchen.planning import create_in_memory_meal_agenda_service
from home_kitchen.recipe_feedback import RecipeFeedbackCreate, RecipeFeedbackService
from home_kitchen.recipes.adapters.memory import MemoryRecipeRepository
from home_kitchen.recipes.bootstrap import create_in_memory_recipe_service
from home_kitchen.recipes.translation import RecipeTranslationPayload
from home_kitchen.service import KitchenService
from home_kitchen.shopping_adapters import MemoryShoppingItemRepository


class _StubIdentityPort:
    """Pure in-memory stub satisfying RecipeIdentityPort and UnmappedIdentityPort."""

    def __init__(self) -> None:
        self.products: dict[UUID, CanonicalProduct] = {}
        self.units: dict[UUID, CanonicalUnit] = {}
        self.aliases: dict[tuple[str, str, str, str], list[UUID]] = {}
        self.recorded_aliases: list[dict[str, Any]] = []

    def add_product(self, name: str, variant: str = "") -> CanonicalProduct:
        prod = CanonicalProduct(id=uuid4(), canonical_name=name, variant=variant)
        self.products[prod.id] = prod
        return prod

    def add_unit(self, name: str) -> CanonicalUnit:
        unit = CanonicalUnit(id=uuid4(), canonical_name=name)
        self.units[unit.id] = unit
        return unit

    def add_alias(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        identity_id: UUID,
        *,
        household_id: UUID | None = None,
        source_type: Any = "automatic_suggestion",
    ) -> None:
        key = (
            kind,
            locale.strip().casefold(),
            context.strip().casefold(),
            alias.strip().casefold(),
        )
        self.aliases.setdefault(key, [])
        if identity_id not in self.aliases[key]:
            self.aliases[key].append(identity_id)
        self.recorded_aliases.append(
            {
                "kind": kind,
                "locale": locale,
                "context": context,
                "alias": alias,
                "identity_id": identity_id,
                "household_id": household_id,
                "source_type": source_type,
            }
        )

    def resolve(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        *,
        household_id: UUID | None = None,
    ) -> AliasResolution:
        del household_id
        key = (
            kind,
            locale.strip().casefold(),
            context.strip().casefold(),
            alias.strip().casefold(),
        )
        ids = self.aliases.get(key, [])
        pool: dict[UUID, Any] = self.products if kind == "product" else self.units
        candidates = [pool[i] for i in ids if i in pool]
        status = (
            "unmapped" if not candidates else "resolved" if len(candidates) == 1 else "ambiguous"
        )
        return AliasResolution(status=status, candidates=candidates)

    def validate_identities(self, kind: str, ids: set[UUID]) -> None:
        valid = set(self.products.keys()) if kind == "product" else set(self.units.keys())
        if not ids <= valid:
            raise ValueError(f"Unknown canonical {kind} identity")


class _StubTranslator:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str]] = []

    async def translate(self, recipe: Recipe, target_locale: str) -> RecipeTranslationPayload:
        self.calls.append((recipe.id, target_locale))
        return RecipeTranslationPayload(
            title=f"{recipe.title} ({target_locale})",
            instructions=[f"[{target_locale}] {step}" for step in recipe.instructions],
            ingredients=recipe.ingredients,
            provider="stub",
            model="stub-model",
        )


@pytest.mark.parametrize(
    ("relative_path", "forbidden_prefixes"),
    [
        (
            "src/home_kitchen/planning/domain/models.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/planning/domain/rules.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/planning/domain/exceptions.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/planning/ports/readers.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/planning/ports/writers.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/planning/ports/recorders.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/planning/ports/repository.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/planning/ports/unit_of_work.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/planning/application/agenda.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/planning/application/service.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/recipes/ports.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store"),
        ),
        (
            "src/home_kitchen/recipes/service.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.history",
                "home_kitchen.settings",
            ),
        ),
        (
            "src/home_kitchen/catalogue/unmapped_service.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/catalogue/adapters.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/recipe_feedback.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store"),
        ),
        (
            "src/home_kitchen/shopping.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.inventory",
                "home_kitchen.history",
            ),
        ),
    ],
)
def test_step5_domain_and_application_modules_have_no_forbidden_imports(
    relative_path: str, forbidden_prefixes: tuple[str, ...]
) -> None:
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        if isinstance(node, ast.ImportFrom) and node.module == "home_kitchen.history":
            names = {alias.name for alias in node.names}
            assert "HistoryRepository" not in names, (
                f"{relative_path} must not import concrete HistoryRepository"
            )
    for mod in imported:
        assert not any(
            mod == prefix or mod.startswith(f"{prefix}.") for prefix in forbidden_prefixes
        ), f"{relative_path} imports forbidden module {mod}"


def test_in_memory_meal_agenda_crud_ordering_and_scaling() -> None:
    recipe = Recipe(
        id=uuid4(),
        title="Carrot soup",
        servings=2,
        ingredients=[Ingredient(name="Carrot", quantity=Decimal("100"), unit="g")],
        instructions=["Cook"],
        source=RecipeSource.MANUAL,
    )
    agenda, meal_repo, _ = create_in_memory_meal_agenda_service(recipes={recipe.id: recipe})

    with pytest.raises(KeyError):
        agenda.plan_meal(MealCreate(date=date(2026, 3, 2), recipe_id=uuid4(), servings=2))

    untimed = agenda.plan_meal(
        MealCreate(date=date(2026, 3, 2), recipe_id=recipe.id, servings=4, label="Middag")
    )
    timed = agenda.plan_meal(
        MealCreate(
            date=date(2026, 3, 2),
            recipe_id=recipe.id,
            servings=2,
            label="Frokost",
            scheduled_time=time(8, 30),
        )
    )
    later_day = agenda.plan_meal(MealCreate(date=date(2026, 3, 4), recipe_id=recipe.id, servings=2))

    listed = agenda.list_meals(date(2026, 3, 2), date(2026, 3, 4))
    assert [m.id for m in listed] == [timed.id, untimed.id, later_day.id]

    with pytest.raises(ValueError, match="end must be on or after start"):
        agenda.list_meals(date(2026, 3, 5), date(2026, 3, 2))

    updated = agenda.update_meal(
        untimed.id,
        MealCreate(date=date(2026, 3, 2), recipe_id=recipe.id, servings=6, label="Festmiddag"),
    )
    assert updated.servings == 6
    assert updated.label == "Festmiddag"

    scaled = agenda.meal_recipe(untimed.id)
    assert scaled.servings == 6
    assert scaled.ingredients[0].quantity == Decimal("300")
    assert meal_repo.has_meals_for_recipe(recipe.id) is True

    agenda.delete_meal(later_day.id)
    with pytest.raises(KeyError):
        agenda.delete_meal(later_day.id)


@pytest.mark.asyncio
async def test_in_memory_recipe_service_crud_identities_stats_and_translations() -> None:
    identities = _StubIdentityPort()
    carrot_prod = identities.add_product("carrot")
    gram_unit = identities.add_unit("g")
    identities.add_alias("product", "nb", "recipe", "gulrot", carrot_prod.id)
    identities.add_alias("unit", "nb", "recipe", "g", gram_unit.id)

    automap_calls: list[tuple[UUID, dict[tuple[UUID, int], set[str]] | None]] = []

    class _StubAutoMapper:
        def auto_map_recipe(
            self,
            recipe_id: UUID,
            *,
            excluded_fields: dict[tuple[UUID, int], set[str]] | None = None,
        ) -> int:
            automap_calls.append((recipe_id, excluded_fields))
            return 0

    translator = _StubTranslator()
    service, repo = create_in_memory_recipe_service(
        identities=identities,
        household_locale="nb",
        translator=translator,
        auto_mapper=_StubAutoMapper(),
    )

    created = service.create_recipe(
        RecipeCreate(
            title="Gulrotsuppe",
            servings=2,
            recipe_type=RecipeType.MIDDAG,
            prep_category=PrepCategory.RASK,
            prep_time_minutes=20,
            ingredients=[
                Ingredient(name="gulrot", quantity=200, unit="g", source_indices=[0]),
                Ingredient(name="mystery spice", quantity=1, unit="ts"),
            ],
            instructions=["Kok gulrot"],
            source_ingredients=[SourceIngredient(index=0, text="200 g gulrot")],
        )
    )
    assert created.ingredients[0].canonical_product_id == carrot_prod.id
    assert created.ingredients[0].canonical_unit_id == gram_unit.id
    assert created.ingredients[0].purchase_estimate is not None
    assert created.ingredients[0].purchase_estimate.quantity == Decimal("200")
    assert len(automap_calls) == 1
    assert automap_calls[0][0] == created.id

    repo.stats[created.id] = RecipeActivityStats(times_planned=2, times_cooked=1, latest_rating=5)
    fetched = service.get_recipe(created.id, include_stats=True)
    assert fetched.activity_stats is not None
    assert fetched.activity_stats.times_cooked == 1

    filtered = service.list_recipes(
        recipe_type="middag", prep_category="rask", max_prep_minutes=25, include_stats=True
    )
    assert [r.id for r in filtered] == [created.id]
    assert service.list_recipes(max_prep_minutes=10) == []

    # Translation workflow + staleness check
    tr = await service.translate_recipe(created.id, "en")
    assert tr.title == "Gulrotsuppe (en)"
    assert tr.is_stale is False
    cached = await service.translate_recipe(created.id, "en")
    assert cached.id == tr.id
    assert len(translator.calls) == 1

    manual = service.update_recipe_translation(
        created.id,
        "en",
        RecipeTranslationUpdate(
            title="Carrot Soup",
            instructions=["Simmer carrots"],
            ingredients=created.ingredients,
        ),
    )
    assert manual.provider == "manual"
    assert service.get_recipe_translation(created.id, "en") == manual

    # Updating the recipe clears explicit identity only if requested, and makes old translation stale
    updated = service.update_recipe(
        created.id,
        RecipeCreate(
            title="Gulrotsuppe v2",
            servings=2,
            ingredients=[
                Ingredient(
                    name="gulrot",
                    quantity=200,
                    unit="g",
                    canonical_product_id=None,
                    canonical_unit_id=gram_unit.id,
                ),
                Ingredient(name="mystery spice", quantity=1, unit="ts"),
            ],
            instructions=["Kok gulrot lenger"],
        ),
    )
    assert updated.ingredients[0].canonical_product_id is None
    assert automap_calls[-1][1] == {(created.id, 0): {"product"}}
    stale = service.get_recipe_translation(created.id, "en")
    assert stale is not None and stale.is_stale is True

    # Delete guard when planned meals exist
    repo.planned_recipe_ids.add(created.id)
    with pytest.raises(ValueError, match="planned meals"):
        service.delete_recipe(created.id)
    repo.planned_recipe_ids.remove(created.id)
    service.delete_recipe(created.id)
    with pytest.raises(KeyError):
        service.get_recipe(created.id)


def test_unmapped_ingredient_and_feedback_services_work_via_narrow_ports() -> None:
    identities = _StubIdentityPort()
    salt_prod = identities.add_product("salt")
    ts_unit = identities.add_unit("ts")

    recipe_service, _ = create_in_memory_recipe_service(identities=identities)
    recipe = recipe_service.create_recipe(
        RecipeCreate(
            title="Broth",
            servings=2,
            ingredients=[Ingredient(name="havsalt", quantity=1, unit="ts")],
            instructions=["Boil"],
        )
    )
    unmapped_svc = UnmappedIngredientService(recipe_service, identities)
    items = unmapped_svc.list_unmapped_ingredients(recipe.id)
    assert len(items) == 1
    assert items[0].name == "havsalt"

    mapped = unmapped_svc.map_unmapped_ingredient(
        recipe.id,
        0,
        canonical_product_id=salt_prod.id,
        canonical_unit_id=ts_unit.id,
        add_alias=True,
        locale="nb",
    )
    assert mapped.ingredients[0].canonical_product_id == salt_prod.id
    assert mapped.ingredients[0].canonical_unit_id == ts_unit.id
    assert unmapped_svc.list_unmapped_ingredients(recipe.id) == []
    assert any(a["source_type"] == "reviewed_correction" for a in identities.recorded_aliases)

    recorded: list[HistoryEntryCreate] = []

    class _StubHistory:
        def record(
            self, data: HistoryEntryCreate, actor_id: str | None = None, connection: Any = None
        ) -> HistoryEntry:
            del connection
            recorded.append(data)
            return HistoryEntry(
                id=uuid4(),
                occurred_at=datetime.now(UTC),
                actor_id=actor_id,
                **data.model_dump(),
            )

    feedback_svc = RecipeFeedbackService(recipe_service, _StubHistory())
    entry = feedback_svc.record(
        recipe.id,
        RecipeFeedbackCreate(rating=5, would_make_again=True, notes="Great broth"),
        channel="mcp",
        actor_id="alice",
    )
    assert entry.kind == "recipe_rating"
    assert len(recorded) == 1
    assert 'Feedback for recipe "Broth".' in recorded[0].message


def test_in_memory_shopping_service_and_kitchen_facade_composition() -> None:
    identities = _StubIdentityPort()
    recipe_repo = MemoryRecipeRepository()
    recipe_service, _ = create_in_memory_recipe_service(recipe_repo, identities=identities)
    meal_agenda, _, _ = create_in_memory_meal_agenda_service(recipes=recipe_repo.recipes)
    shopping_items = MemoryShoppingItemRepository()
    shopping_service, _ = create_in_memory_shopping_service(
        meal_agenda, recipe_service, items=shopping_items
    )
    unmapped_service = UnmappedIngredientService(recipe_service, identities)

    class _DummyStore:
        engine = None
        photo_household_id = None

    facade = KitchenService(
        store=_DummyStore(),
        identity_service=identities,  # type: ignore[arg-type]
        recipe_service=recipe_service,
        meal_agenda=meal_agenda,
        shopping_service=shopping_service,
        unmapped_service=unmapped_service,
    )

    soup = facade.create_recipe(
        RecipeCreate(
            title="Tomato Soup",
            servings=2,
            ingredients=[Ingredient(name="Tomato", quantity=100, unit="g")],
            instructions=["Simmer"],
        )
    )
    # Keep meal_agenda's recipe reader synced with recipe_repo.recipes
    meal = facade.plan_meal(MealCreate(date=date(2026, 4, 1), recipe_id=soup.id, servings=4))
    assert facade.meal_recipe(meal.id).ingredients[0].quantity == Decimal("200")

    milk = facade.add_regular_item(ShoppingItemCreate(name="Milk", quantity=1, unit="l"))
    facade.record_purchase(milk.id, date(2026, 3, 20))
    facade.add_item(ShoppingItemCreate(name="Tomato", quantity=50, unit="g"))

    week = facade.shopping_list(date(2026, 4, 1), date(2026, 4, 1), date(2026, 4, 1))
    assert [(line.name, line.quantity, line.unit) for line in week.lines] == [
        ("Tomato", Decimal("250"), "g")
    ]
    assert [s.id for s in week.suggestions] == [milk.id]

    capture = facade.confirm_suggestion(milk.id)
    assert facade.confirm_suggestion(milk.id).id == capture.id
    facade.complete_item(capture.id, True)
    assert facade.confirm_suggestion(milk.id).id != capture.id

    # Verify MemoryRecipeRepository implements SingleRecipeReader.get_recipe as well
    assert recipe_repo.get_recipe(soup.id).id == soup.id
