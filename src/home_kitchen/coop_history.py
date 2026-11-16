"""Read-only normalization for documented Coop purchase history payloads.

The mapping is based on the reverse-engineered Coop Android 4.17.3 schema.
It does not authenticate, fetch provider data, or assert purchase completion.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict

COOP_SCHEMA_SOURCE = "https://gist.github.com/HelgeSverre/80a7f34f874336324184a0c513c2e6a2"
COOP_SCHEMA_VERSION = "4.17.3"


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


class UnsupportedCoopContract(ValueError):
    """The observed response needs a provider contract not present in the spec."""


class CoopReceiptLine(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=_to_camel, populate_by_name=True)

    product_name: str | None = None
    ean13: str | None = None
    gtin13: str | None = None
    barcode: str | None = None
    quantity: Decimal | None = None
    amount: Decimal | None = None
    discount: Decimal | None = None
    discount_amount_per_quantity: Decimal | None = None
    unit_price: Decimal | None = None


class CoopReceipt(BaseModel):
    """Typed detail receipt retaining provider IDs and nullable source fields."""

    model_config = ConfigDict(extra="forbid", alias_generator=_to_camel, populate_by_name=True)

    summary_id: str | None = None
    receipt_id: str | None = None
    purchase_date: AwareDatetime | None = None
    store_name: str | None = None
    store_id: str | None = None
    chain_id: str | None = None
    amount: Decimal | None = None
    total_discount: Decimal | None = None
    total_discount_formatted: str | None = None
    member_bonus: Decimal | None = None
    lines: list[CoopReceiptLine] | None = None
    source_uri: str = COOP_SCHEMA_SOURCE
    source_schema_version: str = COOP_SCHEMA_VERSION
    source_updated_at: None = None
    revision_id: str


class CoopPurchaseStub(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=_to_camel, populate_by_name=True)

    summary_id: str | None = None
    purchase_date: AwareDatetime | None = None
    store_name: str | None = None
    store_id: str | None = None
    chain_id: str | None = None
    amount: Decimal | None = None
    total_discount: Decimal | None = None


def _revision_id(raw: dict[str, Any]) -> str:
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_purchase_list(
    response: dict[str, Any], *, continuation_observed: bool = False
) -> list[CoopPurchaseStub]:
    """Parse only the documented `{purchases: [...]}` response envelope.

    `continuation_observed` is a local harness signal, not a provider field.
    The gist names a continuation route but does not specify its response shape.
    """
    if continuation_observed:
        raise UnsupportedCoopContract(
            "Coop continuation response schema is undocumented; history cannot be safely continued"
        )
    if not isinstance(response, dict) or set(response) - {"purchases"}:
        raise UnsupportedCoopContract("unsupported Coop purchase-list response shape")
    purchases = response.get("purchases")
    if not isinstance(purchases, list):
        raise UnsupportedCoopContract("Coop purchase-list response must contain a purchases array")
    return [CoopPurchaseStub.model_validate(item) for item in purchases]


def parse_receipt_detail(
    response: dict[str, Any], *, continuation_observed: bool = False
) -> CoopReceipt:
    """Normalize a documented `{summary: PurchaseSummary}` detail response."""
    if continuation_observed:
        raise UnsupportedCoopContract(
            "Coop continuation response schema is undocumented; history cannot be safely continued"
        )
    if not isinstance(response, dict) or set(response) != {"summary"}:
        raise UnsupportedCoopContract("unsupported Coop receipt-detail response shape")
    raw = response["summary"]
    if not isinstance(raw, dict):
        raise UnsupportedCoopContract("Coop receipt-detail summary must be an object")
    return CoopReceipt.model_validate({**raw, "revision_id": _revision_id(raw)})
