"""Typed, provider-backed product and basket review models."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, StringConstraints, ValidationInfo, field_validator

from home_kitchen.models import UnquantifiedNeed

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveQuantity = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class PurchaseNeed(BaseModel):
    id: UUID
    name: NonBlank
    quantity: PositiveQuantity
    unit: NonBlank
    origins: list[str] = Field(default_factory=list)
    category: NonBlank = "Other"
    approximate: bool = False
    provenance: str | None = None
    source_url: HttpUrl | None = None

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str) -> str:
        return value.casefold()


class ProductSnapshot(BaseModel):
    provider: NonBlank = "oda"
    product_id: NonBlank
    name: NonBlank
    brand: NonBlank | None = None
    url: HttpUrl | None = None
    image_url: HttpUrl | None = None
    pack_quantity: PositiveQuantity
    pack_unit: NonBlank
    price: NonNegative | None = None
    currency: NonBlank = "NOK"
    available: bool | None = None
    observed_at: datetime
    evidence_status: Literal["unverified", "provider_verified"] = "unverified"

    @field_validator("url")
    @classmethod
    def validate_product_url(cls, value: HttpUrl | None, info: ValidationInfo) -> HttpUrl | None:
        if value is None:
            return None
        host = value.host or ""
        if value.scheme != "https":
            raise ValueError("Product references must use HTTPS")
        if info.data.get("provider") == "oda" and not (
            host == "oda.com" or host.endswith(".oda.com")
        ):
            raise ValueError("Oda product URLs must use oda.com")
        if value.username is not None or value.password is not None:
            raise ValueError("product URL cannot contain credentials")
        return value

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (value.scheme != "https" or value.username or value.password):
            raise ValueError("image URL must use HTTPS and cannot contain credentials")
        return value

    @field_validator("pack_unit")
    @classmethod
    def normalize_pack_unit(cls, value: str) -> str:
        return value.casefold()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class SelectedProduct(BaseModel):
    need_id: UUID
    product: ProductSnapshot
    packages: int = Field(gt=0)
    alternatives: list[ProductSnapshot] = Field(default_factory=list)


class BasketNeedStatus(BaseModel):
    need: PurchaseNeed
    selections: list[SelectedProduct]
    allocated_quantity: NonNegative
    remaining_quantity: NonNegative
    overbuy_quantity: NonNegative
    ready: bool


class BasketReview(BaseModel):
    unquantified: list[UnquantifiedNeed] = Field(default_factory=list)
    needs: list[BasketNeedStatus]
    total_cost: NonNegative
    price_complete: bool
    currency: NonBlank
    ready_for_review: bool
    issues: list[str]
