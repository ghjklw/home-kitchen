"""Composable contributors build needs; retailer products are a separate feature."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from home_kitchen.models import (
    Meal,
    Recipe,
    RegularItem,
    ShoppingItem,
    ShoppingLine,
    WeekShopping,
)


def normalize_need_text(value: str) -> str:
    """Use one identity for quantity aggregation and stable basket references."""
    return " ".join(value.split()).casefold()


def need_id(start: date, end: date, name: str, unit: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"kitchen:{start}:{end}:{normalize_need_text(name)}:{normalize_need_text(unit)}",
    )


@dataclass(frozen=True)
class ShoppingContext:
    meals: list[Meal]
    recipes: dict[UUID, Recipe]
    captures: list[ShoppingItem]
    regulars: list[RegularItem]
    active_confirmations: set[UUID]
    today: date


@dataclass
class ShoppingContribution:
    lines: list[ShoppingLine] = field(default_factory=list)
    suggestions: list[RegularItem] = field(default_factory=list)


class ShoppingContributor(Protocol):
    contributor_id: str

    def contribute(self, context: ShoppingContext) -> ShoppingContribution: ...


class PlannedRecipes:
    contributor_id = "planned_recipes"

    def contribute(self, context: ShoppingContext) -> ShoppingContribution:
        lines = []
        for meal in context.meals:
            recipe = context.recipes[meal.recipe_id]
            lines.extend(
                ShoppingLine(
                    name=ingredient.name,
                    quantity=ingredient.quantity
                    * Decimal(meal.servings)
                    / Decimal(recipe.servings),
                    unit=ingredient.unit,
                    category=ingredient.category,
                    origins=[f"meal:{meal.id}"],
                )
                for ingredient in recipe.ingredients
            )
        return ShoppingContribution(lines=lines)


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
        for contributor in self.contributors.values():
            contribution = contributor.contribute(context)
            for line in contribution.lines:
                key = (normalize_need_text(line.name), normalize_need_text(line.unit))
                if key not in totals:
                    totals[key] = line.model_copy(deep=True)
                else:
                    totals[key].quantity += line.quantity
                    if totals[key].category == "Other":
                        totals[key].category = line.category
                    totals[key].origins = list(dict.fromkeys(totals[key].origins + line.origins))
            suggestions.update({item.id: item for item in contribution.suggestions})
        return WeekShopping(
            lines=sorted(totals.values(), key=lambda item: (item.name.casefold(), item.unit)),
            suggestions=list(suggestions.values()),
        )


def default_shopping_builder() -> ShoppingBuilder:
    return ShoppingBuilder([PlannedRecipes(), QuickAdditions(), RegularItems()])
