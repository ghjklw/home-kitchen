"""Composable contributors build needs; retailer products are a separate feature."""

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from home_kitchen.models import (
    Meal,
    Recipe,
    RegularItem,
    ShoppingItem,
    ShoppingLine,
    ShoppingSourceQuantity,
    UnquantifiedNeed,
    WeekShopping,
)
from home_kitchen.units import add_exact, canonical_unit, canonicalize_quantity, scale_quantity


def normalize_need_text(value: str) -> str:
    """Use one identity for quantity aggregation and stable basket references."""
    return " ".join(value.split()).casefold()


def need_id(start: date, end: date, name: str, unit: str) -> UUID:
    """Canonical units keep equivalent needs stable; legacy-unit basket selections become stale.

    Selection records with pre-canonical unit IDs are intentionally not migrated implicitly.
    The basket review reports them as stale so a user must review the converted need.
    """
    return uuid5(
        NAMESPACE_URL,
        f"kitchen:{start}:{end}:{normalize_need_text(name)}:{canonical_unit(unit)}",
    )


# Window, need, source kind, source ID: closing one source never closes another
# source for the same product or a requirement in a later shopping window.
ClosedSourceKey = tuple[date, date, UUID, str, UUID]


@dataclass(frozen=True)
class ShoppingContext:
    meals: list[Meal]
    recipes: dict[UUID, Recipe]
    captures: list[ShoppingItem]
    regulars: list[RegularItem]
    active_confirmations: set[UUID]
    today: date
    start: date | None = None
    end: date | None = None
    closed_sources: set[ClosedSourceKey] = field(default_factory=set)


@dataclass
class ShoppingContribution:
    lines: list[ShoppingLine] = field(default_factory=list)
    suggestions: list[RegularItem] = field(default_factory=list)
    unquantified: list[UnquantifiedNeed] = field(default_factory=list)


class ShoppingContributor(Protocol):
    contributor_id: str

    def contribute(self, context: ShoppingContext) -> ShoppingContribution: ...


class PlannedRecipes:
    contributor_id = "planned_recipes"

    def contribute(self, context: ShoppingContext) -> ShoppingContribution:
        result = ShoppingContribution()
        for meal in context.meals:
            recipe = context.recipes[meal.recipe_id]
            for ingredient in recipe.ingredients:
                estimate = ingredient.purchase_estimate
                quantity = estimate.quantity if estimate is not None else ingredient.quantity
                unit = estimate.unit if estimate is not None else ingredient.unit
                if (
                    quantity is None
                    or unit is None
                    or unit
                    in {
                        "klype",
                        "håndfull",
                        "etter smak",
                        "ss",
                        "ts",
                    }
                    and estimate is None
                ):
                    result.unquantified.append(
                        UnquantifiedNeed(
                            name=ingredient.name,
                            unit=unit,
                            category=ingredient.category,
                            origins=[f"meal:{meal.id}"],
                            source_indices=ingredient.source_indices,
                        )
                    )
                    continue
                # Ingredient validation guarantees a unit for measured amounts.
                assert unit is not None
                scaled_quantity = scale_quantity(quantity, meal.servings, recipe.servings)
                result.lines.append(
                    ShoppingLine(
                        name=ingredient.name,
                        quantity=scaled_quantity,
                        unit=unit,
                        category=ingredient.category,
                        origins=[f"meal:{meal.id}"],
                        source_quantities=[
                            ShoppingSourceQuantity(
                                kind="meal", source_id=meal.id, quantity=scaled_quantity, unit=unit
                            )
                        ],
                        approximate=estimate.approximate if estimate else False,
                        provenance=estimate.provenance if estimate else None,
                        source_url=estimate.source_url if estimate else None,
                    )
                )
        return result


class QuickAdditions:
    contributor_id = "quick_additions"

    def contribute(self, context: ShoppingContext) -> ShoppingContribution:
        return ShoppingContribution(
            lines=[
                ShoppingLine(
                    name=item.name,
                    quantity=item.quantity,
                    unit=item.unit,
                    category=item.category,
                    origins=[f"item:{item.id}"],
                    source_quantities=[
                        ShoppingSourceQuantity(
                            kind="capture",
                            source_id=item.id,
                            quantity=item.quantity,
                            unit=item.unit,
                        )
                    ],
                )
                for item in context.captures
                if not item.completed
            ]
        )


class RegularItems:
    contributor_id = "regular_items"

    def contribute(self, context: ShoppingContext) -> ShoppingContribution:
        result = ShoppingContribution()
        for item in context.regulars:
            if item.id in context.active_confirmations:
                continue
            if item.last_purchased is None:
                result.lines.append(
                    ShoppingLine(
                        name=item.name,
                        quantity=item.quantity,
                        unit=item.unit,
                        category=item.category,
                        origins=[f"regular:{item.id}"],
                        source_quantities=[
                            ShoppingSourceQuantity(
                                kind="regular",
                                source_id=item.id,
                                quantity=item.quantity,
                                unit=item.unit,
                            )
                        ],
                    )
                )
            elif (context.today - item.last_purchased).days >= item.interval_days:
                result.suggestions.append(item)
        return result


class ShoppingBuilder:
    def __init__(self, contributors: list[ShoppingContributor]):
        self.contributors: dict[str, ShoppingContributor] = {}
        for contributor in contributors:
            self.register(contributor)

    def register(self, contributor: ShoppingContributor) -> None:
        if contributor.contributor_id in self.contributors:
            raise ValueError("Shopping contributor already registered")
        self.contributors[contributor.contributor_id] = contributor

    def build(self, context: ShoppingContext) -> WeekShopping:
        totals: dict[tuple[str, str], ShoppingLine] = {}
        suggestions: dict[UUID, RegularItem] = {}
        unquantified: list[UnquantifiedNeed] = []
        for contributor in self.contributors.values():
            contribution = contributor.contribute(context)
            for line in contribution.lines:
                quantity, unit = canonicalize_quantity(line.quantity, line.unit)
                sources = []
                origins = list(line.origins)
                for source in line.source_quantities:
                    source_quantity, source_unit = canonicalize_quantity(
                        source.quantity, source.unit
                    )
                    if context.start is not None and context.end is not None:
                        source_key = (
                            context.start,
                            context.end,
                            need_id(context.start, context.end, line.name, unit),
                            source.kind,
                            source.source_id,
                        )
                        if source_key in context.closed_sources:
                            quantity = add_exact(quantity, -source_quantity)
                            origin_kind = "item" if source.kind == "capture" else source.kind
                            closed_origin = f"{origin_kind}:{source.source_id}"
                            origins = [origin for origin in origins if origin != closed_origin]
                            continue
                    sources.append(
                        source.model_copy(update={"quantity": source_quantity, "unit": source_unit})
                    )
                if quantity <= 0:
                    continue
                key = (normalize_need_text(line.name), unit)
                if key not in totals:
                    totals[key] = line.model_copy(
                        update={
                            "quantity": quantity,
                            "unit": unit,
                            "source_quantities": sources,
                            "origins": origins,
                        },
                        deep=True,
                    )
                else:
                    totals[key].quantity = add_exact(totals[key].quantity, quantity)
                    for source in sources:
                        existing = next(
                            (
                                item
                                for item in totals[key].source_quantities
                                if (item.kind, item.source_id, item.unit)
                                == (source.kind, source.source_id, source.unit)
                            ),
                            None,
                        )
                        if existing is None:
                            totals[key].source_quantities.append(source.model_copy(deep=True))
                        else:
                            existing.quantity = add_exact(existing.quantity, source.quantity)
                    if totals[key].category == "Other":
                        totals[key].category = line.category
                    totals[key].origins = list(dict.fromkeys(totals[key].origins + origins))
                    totals[key].approximate = totals[key].approximate or line.approximate
                    if line.provenance and line.provenance not in (totals[key].provenance or ""):
                        totals[key].provenance = "; ".join(
                            value for value in (totals[key].provenance, line.provenance) if value
                        )
                    if totals[key].source_url is None:
                        totals[key].source_url = line.source_url
                    elif line.source_url and line.source_url != totals[key].source_url:
                        totals[key].provenance = "; ".join(
                            value
                            for value in (
                                totals[key].provenance,
                                f"Additional source: {line.source_url}",
                            )
                            if value
                        )
            suggestions.update({item.id: item for item in contribution.suggestions})
            unquantified.extend(contribution.unquantified)
        return WeekShopping(
            lines=sorted(totals.values(), key=lambda item: (item.name.casefold(), item.unit)),
            suggestions=list(suggestions.values()),
            unquantified=unquantified,
        )


def default_shopping_builder() -> ShoppingBuilder:
    return ShoppingBuilder([PlannedRecipes(), QuickAdditions(), RegularItems()])
