"""Canonical product and unit identities, separate from source evidence."""

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


def normalize_identity_text(value: str) -> str:
    return " ".join(value.split()).casefold()
