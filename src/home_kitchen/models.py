"""Validated household kitchen data."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, StringConstraints, field_validator

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Quantity = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class Ingredient(BaseModel):
    name: NonBlank
    quantity: Quantity
    unit: NonBlank = "item"

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str) -> str:
        return value.casefold()


class RecipeSource(StrEnum):
    MANUAL = "manual"
    IMPORTED = "imported"
    AI = "ai"


class RecipeCreate(BaseModel):
    title: NonBlank
    servings: int = Field(gt=0)
    ingredients: list[Ingredient] = Field(min_length=1)
    instructions: list[NonBlank] = Field(min_length=1)
    source_url: HttpUrl | None = None
    source: RecipeSource = RecipeSource.MANUAL


class Recipe(RecipeCreate):
    id: UUID


class MealCreate(BaseModel):
    date: date
    slot: Literal["breakfast", "lunch", "dinner"] = "dinner"
    recipe_id: UUID
    servings: int = Field(gt=0)


class Meal(MealCreate):
    id: UUID


class ShoppingItemCreate(BaseModel):
    name: NonBlank
    quantity: Quantity = Decimal("1")
    unit: NonBlank = "item"

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str) -> str:
        return value.casefold()


class ShoppingItem(ShoppingItemCreate):
    id: UUID
    completed: bool = False


class RegularItem(ShoppingItemCreate):
    id: UUID
    interval_days: int = Field(default=7, gt=0)
    last_purchased: date | None = None


class ShoppingLine(BaseModel):
    name: NonBlank
    quantity: Quantity
    unit: NonBlank
    origins: list[str]


class WeekShopping(BaseModel):
    lines: list[ShoppingLine]
    suggestions: list[RegularItem]
