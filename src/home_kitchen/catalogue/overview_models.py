"""Models for the household Product Overview read model and preferences."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, field_validator

from home_kitchen.catalogue.identity import AliasSourceType
from home_kitchen.units import normalize_unit

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProductAliasItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: NonBlank
    locale: NonBlank
    context: str = ""
    reviewed: bool = False
    source_type: AliasSourceType = "reviewed_correction"


class PreferredOdaSku(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oda_product_id: NonBlank
    rank: int = Field(ge=1)
    name: str | None = None
    brand: str | None = None
    price: str | None = None
    unit_price: str | None = None
    unit_name: str | None = None
    url: HttpUrl | None = None
    image_url: HttpUrl | None = None


class PreferredOdaSkuInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oda_product_id: NonBlank
    rank: int | None = Field(default=None, ge=1)
    name: str | None = None
    brand: str | None = None
    price: str | None = None
    unit_price: str | None = None
    unit_name: str | None = None
    url: HttpUrl | None = None
    image_url: HttpUrl | None = None


class ExcludedOdaSku(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oda_product_id: NonBlank
    name: str | None = None
    reason: str = ""
    excluded_at: datetime | None = None


class ExcludedOdaSkuInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oda_product_id: NonBlank
    name: str | None = None
    reason: str = ""


class ProductPreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_oda_skus: list[PreferredOdaSkuInput | NonBlank] = Field(default_factory=list)
    excluded_oda_skus: list[ExcludedOdaSkuInput | NonBlank] = Field(default_factory=list)


class RememberedOdaSku(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oda_product_id: NonBlank
    provider: str = "oda"
    name: str | None = None
    brand: str | None = None
    price: str | None = None
    unit_name: str | None = None
    url: HttpUrl | None = None
    image_url: HttpUrl | None = None
    last_selected_at: datetime | None = None
    selection_count: int = Field(default=1, ge=1)
    source: str = "basket_selection"


class PreviouslyPurchasedSku(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku_id: NonBlank
    provider: NonBlank
    name: str | None = None
    store_name: str | None = None
    ean: str | None = None
    last_purchased_at: datetime | None = None
    purchase_count: int = Field(default=1, ge=1)
    provenance: Literal["provider_verified", "user_confirmed", "uncertain"] = "provider_verified"


class ProductMergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_product_id: UUID


class ProductMergePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_product_id: UUID
    source_product_id: UUID
    target_canonical_name: NonBlank
    source_canonical_name: NonBlank
    target_variant: str = ""
    source_variant: str = ""
    target_display_name: NonBlank
    source_display_name: NonBlank
    variant_mismatch: bool
    aliases_to_move: int = Field(default=0, ge=0)
    stock_rows_to_move: int = Field(default=0, ge=0)
    recipe_links_to_move: int = Field(default=0, ge=0)
    preferences_to_move: int = Field(default=0, ge=0)
    exclusions_to_move: int = Field(default=0, ge=0)
    purchase_history_rows_to_move: int = Field(default=0, ge=0)


class ProductStockUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: Decimal = Field(ge=0, allow_inf_nan=False)
    unit: NonBlank = "stk"
    category: NonBlank = "Other"
    note: str | None = Field(default=None, max_length=2000)
    inventory_id: UUID | None = None

    @field_validator("unit")
    @classmethod
    def normalized_unit(cls, value: str) -> str:
        return normalize_unit(value)


class StockItemDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inventory_id: UUID | None = None
    name: NonBlank
    quantity: Decimal = Field(ge=0)
    unit: NonBlank
    category: str = "Other"
    note: str | None = None
    provenance: Literal["kitchen_inventory"] = "kitchen_inventory"


class ProductStockOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_stock: bool
    total_quantity: Decimal = Decimal(0)
    unit: str | None = None
    items: list[StockItemDetail] = Field(default_factory=list)
    provenance: Literal["kitchen_inventory"] = "kitchen_inventory"


class ProvenanceCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_verified: int = 0
    user_confirmed: int = 0
    uncertain: int = 0


class PurchaseEvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str | None = None
    purchased_at: datetime
    provenance: Literal["provider_verified", "user_confirmed", "uncertain"]
    evidence_source: str
    evidence_ref: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    product_name: str | None = None
    oda_product_id: str | None = None


class PurchaseFrequencyOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["known", "unknown"]
    average_interval_days: float | None = None
    sample_size: int = 0
    first_purchased_at: datetime | None = None
    last_purchased_at: datetime | None = None
    date_range_days: int | None = None
    frequency_display: str
    counts_by_provenance: ProvenanceCounts = Field(default_factory=ProvenanceCounts)
    evidence_history: list[PurchaseEvidenceRecord] = Field(default_factory=list)


class ProductOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    canonical_name: NonBlank
    variant: str = ""
    display_name: NonBlank
    locale: str = "nb"
    aliases: list[ProductAliasItem] = Field(default_factory=list)
    translations_by_locale: dict[str, list[str]] = Field(default_factory=dict)
    preferred_oda_skus: list[PreferredOdaSku] = Field(default_factory=list)
    excluded_oda_skus: list[ExcludedOdaSku] = Field(default_factory=list)
    remembered_oda_skus: list[RememberedOdaSku] = Field(default_factory=list)
    previously_purchased_skus: list[PreviouslyPurchasedSku] = Field(default_factory=list)
    stock: ProductStockOverview
    purchase_frequency: PurchaseFrequencyOverview
