"""Validated household kitchen data."""

from datetime import date, time
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, StringConstraints, field_validator, model_validator

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Quantity = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
SourceIndex = Annotated[int, Field(ge=0)]


class PurchaseEstimate(BaseModel):
    """A separate shopping amount, with explicit approximation evidence."""

    quantity: Quantity
    unit: NonBlank
    approximate: bool
    provenance: NonBlank
    source_url: HttpUrl | None = None

    @field_validator("unit")
    @classmethod
    def normalize_estimate_unit(cls, value: str) -> str:
        return value.casefold()


class Ingredient(BaseModel):
    name: NonBlank
    quantity: Quantity | None
    unit: NonBlank | None = "item"
    canonical_product_id: UUID | None = None
    canonical_unit_id: UUID | None = None
    source_unit_expression: NonBlank | None = None
    category: NonBlank = "Other"
    source_indices: list[SourceIndex] = Field(default_factory=list)
    purchase_estimate: PurchaseEstimate | None = None

    @model_validator(mode="wrap")
    @classmethod
    def retain_source_unit_expression(cls, value: Any, handler: Any) -> Any:
        ingredient = handler(value)
        if isinstance(value, dict) and "source_unit_expression" not in value:
            object.__setattr__(ingredient, "source_unit_expression", value.get("unit"))
        return ingredient

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str | None) -> str | None:
        return value.casefold() if value is not None else None

    @model_validator(mode="after")
    def measured_ingredient_requires_unit(self) -> "Ingredient":
        if self.quantity is not None and self.unit is None:
            raise ValueError("Measured ingredients require a unit")
        return self


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


class PhotoSource(BaseModel):
    """Private source identity; caller-supplied book attribution is unverified."""

    id: UUID
    book_title: NonBlank | None = Field(default=None, max_length=300)
    page: NonBlank | None = Field(default=None, max_length=80)


class RecipeImageAttribution(BaseModel):
    """Provider attribution for an explicitly selected public recipe image."""

    provider: Literal["wikimedia_commons"] = "wikimedia_commons"
    provider_id: NonBlank = Field(max_length=80)
    query: NonBlank = Field(max_length=200)
    title: NonBlank = Field(max_length=300)
    image_url: HttpUrl
    source_page_url: HttpUrl
    artist: str | None = Field(default=None, max_length=500)
    credit: str | None = Field(default=None, max_length=500)
    license_name: NonBlank = Field(max_length=100)
    license_url: HttpUrl
    attribution_required: bool
    attribution: str | None = Field(default=None, max_length=500)
    obligations: list[NonBlank] = Field(default_factory=list, max_length=4)


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
    photo_source: PhotoSource | None = None
    image_attribution: RecipeImageAttribution | None = None

    @model_validator(mode="after")
    def valid_source_metadata(self) -> "RecipeCreate":
        validate_source_metadata(
            self.source_ingredients,
            [item.source_indices for item in self.ingredients],
            self.source_url,
            self.provenance,
        )
        if self.image_attribution and self.image_url != self.image_attribution.image_url:
            # A caller may be editing the image URL while echoing a prior Recipe
            # response. Drop stale credit; only the selection service may attach it.
            self.image_attribution = None
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
    recipe_id: UUID
    servings: int = Field(gt=0)
    label: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)] | None
    ) = None
    scheduled_time: time | None = None

    @field_validator("scheduled_time")
    @classmethod
    def validate_local_scheduled_time(cls, value: time | None) -> time | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("Scheduled time must be a local time without a timezone")
        return value

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_slot(cls, value: Any) -> Any:
        """Translate old breakfast/lunch/dinner payloads and pending proposals."""
        if isinstance(value, dict) and "slot" in value:
            data = dict(value)
            slot = data.pop("slot")
            if data.get("label") is None and slot is not None:
                data["label"] = {
                    "breakfast": "Frokost",
                    "lunch": "Lunsj",
                    "dinner": "Middag",
                }.get(str(slot).casefold(), str(slot))
            return data
        return value


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


class ShoppingSourceQuantity(BaseModel):
    """One source's planned contribution, independent of any actual purchase."""

    kind: Literal["meal", "capture", "regular"]
    source_id: UUID
    quantity: Quantity
    unit: NonBlank


class ShoppingLine(BaseModel):
    name: NonBlank
    quantity: Quantity
    unit: NonBlank
    category: NonBlank = "Other"
    origins: list[str]
    source_quantities: list[ShoppingSourceQuantity] = Field(default_factory=list)
    approximate: bool = False
    provenance: NonBlank | None = None
    source_url: HttpUrl | None = None


class UnquantifiedNeed(BaseModel):
    """An explicit requirement whose quantity still needs human review."""

    name: NonBlank
    unit: NonBlank | None = None
    category: NonBlank = "Other"
    origins: list[str]
    source_indices: list[SourceIndex] = Field(default_factory=list)


class WeekShopping(BaseModel):
    lines: list[ShoppingLine]
    suggestions: list[RegularItem]
    unquantified: list[UnquantifiedNeed] = Field(default_factory=list)
