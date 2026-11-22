"""Provider-neutral receipt models and aggregation helpers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ReceiptLine(BaseModel):
    """A receipt line represented with provider-neutral field names."""

    model_config = ConfigDict(extra="forbid")

    name: str
    raw_name: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    amount: Decimal | None = None
    ean: str | None = None
    canonical_product_id: str | None = None
    canonical_unit_id: str | None = None
    oda_product_id: str | None = None
    category: str | None = None
    is_return: bool = False


class Receipt(BaseModel):
    """A provider-neutral receipt that can be aggregated across retailers."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["oda", "coop", "trumf", "rema1000"]
    external_id: str | None = None
    revision_id: str | None = None
    transaction_at: date | datetime | None = None
    store_name: str | None = None
    store_id: str | None = None
    chain: str | None = None
    total_amount: Decimal | None = None
    total_discount: Decimal | None = None
    currency: str | None = None
    has_receipt: bool = True
    lines: list[ReceiptLine] = Field(default_factory=list)
    source_uri: str | None = None
    source_schema_version: str | None = None
    source_updated_at: datetime | None = None


class StandardizableReceipt(Protocol):
    def standardize(self) -> Receipt: ...


class ReceiptCollection(BaseModel):
    """A collection of standardized receipts from any supported retailer."""

    model_config = ConfigDict(extra="forbid")

    receipts: list[Receipt] = Field(default_factory=list)

    @classmethod
    def from_retailer_receipts(
        cls, receipts: Iterable[StandardizableReceipt]
    ) -> ReceiptCollection:
        return cls(receipts=[receipt.standardize() for receipt in receipts])
