"""Exact retailer SKU snapshots and household curation commands."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, StringConstraints, field_validator, model_validator

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class RetailerProductSnapshot(BaseModel):
    """One observed retailer product; identifiers are exact and never fuzzy-matched."""

    provider: NonBlank
    product_id: NonBlank
    name: NonBlank
    url: HttpUrl | None = None
    image_url: HttpUrl | None = None
    raw_pack: dict | str | None = None
    quantity: Positive | None = None
    quantity_unit: NonBlank | None = None
    snapshot_ref: NonBlank | None = None
    observed_at: datetime
    evidence_status: Literal["unverified", "provider_verified"] = "unverified"

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value

    @field_validator("url", "image_url")
    @classmethod
    def require_safe_https(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (
            value.scheme != "https" or value.username is not None or value.password is not None
        ):
            raise ValueError("product URLs must use HTTPS and cannot contain credentials")
        return value

    @model_validator(mode="after")
    def quantity_unit_are_paired(self):
        if (self.quantity is None) != (self.quantity_unit is None):
            raise ValueError("quantity and quantity_unit must be supplied together")
        return self


class RetailerProductOption(BaseModel):
    id: UUID
    snapshot_id: UUID
    household_id: UUID
    food_catalogue_id: UUID
    provider: str
    product_id: str
    snapshot: RetailerProductSnapshot
    preferred: bool = False


class ExplicitPurchaseEvidence(BaseModel):
    """Purchase facts can only be recorded with a concrete, caller-supplied source."""

    evidence_kind: Literal["retailer_order", "receipt", "manual_confirmation"]
    evidence_ref: NonBlank
    purchased_at: datetime
    quantity: Positive | None = None
    quantity_unit: NonBlank | None = None

    @field_validator("purchased_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("purchased_at must be timezone-aware")
        if value.astimezone(UTC) > datetime.now(UTC):
            raise ValueError("purchased_at cannot be in the future")
        return value

    @model_validator(mode="after")
    def quantity_unit_are_paired(self):
        if (self.quantity is None) != (self.quantity_unit is None):
            raise ValueError("quantity and quantity_unit must be supplied together")
        return self
