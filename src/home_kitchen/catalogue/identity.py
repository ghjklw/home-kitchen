"""Canonical product and unit identities, separate from source evidence."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from home_kitchen.models import NonBlank


class CanonicalProduct(BaseModel):
    id: UUID
    canonical_name: NonBlank
    variant: str = ""


class CanonicalUnit(BaseModel):
    id: UUID
    canonical_name: NonBlank


class AliasInput(BaseModel):
    alias: NonBlank
    locale: NonBlank
    context: str = ""


class ProductIdentityCreate(BaseModel):
    canonical_name: NonBlank
    variant: str = ""


class UnitIdentityCreate(BaseModel):
    canonical_name: NonBlank


class AliasResolution(BaseModel):
    status: Literal["unmapped", "resolved", "ambiguous"]
    candidates: list[CanonicalProduct | CanonicalUnit] = Field(default_factory=list)


class UnmappedIngredient(BaseModel):
    recipe_id: UUID
    recipe_title: str
    ingredient_index: int
    name: NonBlank
    quantity: Decimal | None = None
    unit: str | None = None
    source_unit_expression: str | None = None
    canonical_product_id: UUID | None = None
    canonical_unit_id: UUID | None = None


class MapIngredientInput(BaseModel):
    recipe_id: UUID
    ingredient_index: int = Field(ge=0)
    canonical_product_id: UUID | None = None
    canonical_unit_id: UUID | None = None
    add_alias: bool = True
    locale: str = Field(default="nb", min_length=2, max_length=20)
    context: str = Field(default="recipe", max_length=100)


def normalize_identity_text(value: str) -> str:
    return " ".join(value.split()).casefold()
