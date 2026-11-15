"""Validated household kitchen data."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, StringConstraints, field_validator, model_validator

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Quantity = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
SourceIndex = Annotated[int, Field(ge=0)]


class Ingredient(BaseModel):
    name: NonBlank
    quantity: Quantity
    unit: NonBlank = "item"
    category: NonBlank = "Other"
    source_indices: list[SourceIndex] = Field(default_factory=list)

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str) -> str:
        return value.casefold()


class RecipeSource(StrEnum):
    MANUAL = "manual"
    IMPORTED = "imported"
    AI = "ai"


class SourceIngredient(BaseModel):
    index: SourceIndex
    text: NonBlank


class AdaptationProvenance(BaseModel):
    """Attribution supplied for review, never certification of source authenticity."""

    kind: Literal["ai_adaptation"] = "ai_adaptation"
    original_source: RecipeSource
    source_url: HttpUrl | None = None


def validate_source_metadata(
    source_ingredients: list[SourceIngredient],
    ingredient_references: list[list[int]],
    source_url: HttpUrl | None,
    provenance: AdaptationProvenance | None,
) -> None:
    indices = [item.index for item in source_ingredients]
    if len(set(indices)) != len(indices):
        raise ValueError("Source ingredient indices must be unique")
    if any(index not in indices for references in ingredient_references for index in references):
        raise ValueError("Ingredient source_indices must reference source_ingredients")
    if provenance is not None and provenance.source_url != source_url:
        raise ValueError("Provenance source_url must match recipe source_url")


class RecipeCreate(BaseModel):
    title: NonBlank
    servings: int = Field(gt=0)
    ingredients: list[Ingredient] = Field(min_length=1)
    instructions: list[NonBlank] = Field(min_length=1)
    source_url: HttpUrl | None = None
    image_url: HttpUrl | None = None
    source: RecipeSource = RecipeSource.MANUAL
    source_ingredients: list[SourceIngredient] = Field(default_factory=list)
    provenance: AdaptationProvenance | None = None

    @model_validator(mode="after")
    def valid_source_metadata(self) -> "RecipeCreate":
        validate_source_metadata(
            self.source_ingredients,
            [item.source_indices for item in self.ingredients],
            self.source_url,
            self.provenance,
        )
        return self

    @field_validator("image_url")
    @classmethod
    def safe_image_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value and (value.scheme != "https" or value.username or value.password):
            raise ValueError("Recipe images must use public HTTPS URLs without credentials")
        return value


class Recipe(RecipeCreate):
    id: UUID


class MealCreate(BaseModel):
    date: date
    slot: Literal["breakfast", "lunch", "dinner"] = "dinner"
    recipe_id: UUID
    servings: int = Field(gt=0)


class Meal(MealCreate):
    id: UUID


class PlannedMealRecipe(BaseModel):
    """Cooking quantities for one meal, alongside its unchanged source recipe."""

    meal: Meal
    source_recipe: Recipe
    servings: int = Field(gt=0)
    ingredients: list[Ingredient]


class ShoppingItemCreate(BaseModel):
    name: NonBlank
    quantity: Quantity = Decimal("1")
    unit: NonBlank = "item"
    category: NonBlank = "Other"

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
    category: NonBlank = "Other"
    origins: list[str]


class WeekShopping(BaseModel):
    lines: list[ShoppingLine]
    suggestions: list[RegularItem]
