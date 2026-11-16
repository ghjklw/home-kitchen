"""Oda source snapshots only; even delivered orders are not purchase evidence.

The manifest supplies no revision, source update time, ordering or incremental
watermark. Polls therefore scan from page one and hash provider content only.
"""

from __future__ import annotations

import asyncio
import json
from base64 import b64decode, urlsafe_b64encode
from datetime import date
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)

from home_kitchen.purchase_sync import ProviderOrderRecord, ProviderPage

if TYPE_CHECKING:
    from .service import OdaService

OrderStatus = Literal[
    "unpaid_order",
    "unpaid_order_change",
    "paid_and_modifiable",
    "paid_and_not_modifiable",
    "picking",
    "shipped",
    "delivered",
    "failed",
    "cancelled",
]


class SourceModel(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class OrderProduct(SourceModel):
    id: StrictInt
    name: StrictStr
    description: StrictStr
    price: StrictStr
    unitPrice: StrictStr
    unitName: StrictStr


class OrderLine(SourceModel):
    product: OrderProduct
    quantity: StrictInt | StrictFloat
    totalGrossAmount: StrictStr


class Order(SourceModel):
    orderNumber: StrictStr = Field(min_length=1, max_length=512)
    deliveryDate: date
    currency: StrictStr
    grossAmount: StrictInt | StrictFloat
    products: list[OrderLine]

    @field_validator("deliveryDate", mode="before")
    @classmethod
    def delivery_date_must_be_iso_string(cls, value: object) -> object:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("deliveryDate must be an ISO date string")
        date.fromisoformat(value)
        return value


class OrdersResponse(SourceModel):
    hasMore: StrictBool
    orders: list[Order] = Field(max_length=20)


class TrackingResponse(SourceModel):
    orderNumber: StrictStr
    status: OrderStatus


def validate_history_tool(name: str, schema: dict, output: dict) -> None:
    """Fail closed on changes to the read contracts used by this adapter."""
    properties = schema.get("properties", {})
    required = {"order_number"} if name == "order_tracking" else set()
    fields = {"order_number"} if required else {"page", "size"}
    if set(schema.get("required", [])) != required or not fields <= set(properties) <= fields | {
        "intent"
    }:
        raise ValueError("changed input schema")
    if required:
        if properties["order_number"].get("type") != "string":
            raise ValueError("changed order number schema")
    else:
        for field, maximum in (("page", 50), ("size", 20)):
            prop = properties[field]
            if (prop.get("type"), prop.get("minimum"), prop.get("maximum")) != (
                "integer",
                1,
                maximum,
            ):
                raise ValueError("changed pagination schema")

    def check_object(node: dict, expected: dict) -> None:
        if node.get("type") != "object" or set(node.get("required", [])) != set(expected):
            raise ValueError("changed output schema")
        for field, kind in expected.items():
            prop = node.get("properties", {}).get(field, {})
            if prop.get("type") != kind:
                raise ValueError("changed output field schema")

    if required:
        check_object(output, {"orderNumber": "string", "status": "string"})
        if set(output["properties"]["status"].get("enum", [])) != set(OrderStatus.__args__):
            raise ValueError("changed lifecycle statuses")
    else:
        check_object(output, {"hasMore": "boolean", "orders": "array"})
        order = output["properties"]["orders"].get("items", {})
        check_object(
            order,
            {
                "orderNumber": "string",
                "deliveryDate": "string",
                "currency": "string",
                "grossAmount": "number",
                "products": "array",
            },
        )
        if order["properties"]["deliveryDate"].get("format") != "date":
            raise ValueError("changed delivery date schema")
        line = order["properties"]["products"].get("items", {})
        check_object(
            line, {"product": "object", "quantity": "number", "totalGrossAmount": "string"}
        )
        check_object(
            line["properties"]["product"],
            {
                "id": "integer",
                "name": "string",
                "description": "string",
                "price": "string",
                "unitPrice": "string",
                "unitName": "string",
            },
        )


class _ScanCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    account_id: str
    scan_id: str
    next_page: int = Field(ge=1, le=50)
    state: Literal["scanning", "complete", "incomplete"]


def _encode(cursor: _ScanCursor) -> str:
    return "oda-scan-v1." + urlsafe_b64encode(cursor.model_dump_json().encode()).decode()


def _decode(value: str) -> _ScanCursor:
    try:
        prefix, encoded = value.split(".", 1)
        if prefix != "oda-scan-v1" or len(value) > 2048:
            raise ValueError
        cursor = _ScanCursor.model_validate_json(b64decode(encoded, altchars=b"-_", validate=True))
        UUID(cursor.scan_id)
        if cursor.state != "scanning" and cursor.next_page != 1:
            raise ValueError
        return cursor
    except (ValueError, TypeError):
        raise ValueError("Invalid Oda history scan cursor") from None


class OdaPurchaseHistoryAdapter:
    """Bound to one local actor's Oda connection; no household effects or writes."""

    def __init__(self, service: OdaService, account_id: str, *, fetch_tracking: bool = True):
        if not account_id.strip() or account_id != account_id.strip():
            raise ValueError("Oda account_id must be non-empty and trimmed")
        self.service = service
        self.account_id = account_id
        self.fetch_tracking = fetch_tracking

    def fetch_page(self, account_id: str, cursor: str | None) -> ProviderPage:
        return asyncio.run(self.fetch_page_async(account_id, cursor))

    async def fetch_page_async(self, account_id: str, cursor: str | None) -> ProviderPage:
        if account_id != self.account_id:
            raise ValueError("Oda history adapter account mismatch")
        scan = _decode(cursor) if cursor is not None else None
        if scan is not None and scan.account_id != account_id:
            raise ValueError("Oda history cursor account mismatch")
        if scan is None or scan.state != "scanning":
            scan = _ScanCursor(
                version=1,
                account_id=account_id,
                scan_id=str(uuid4()),
                next_page=1,
                state="scanning",
            )
        response = await self.service.list_orders(account_id, page=scan.next_page, size=20)
        # Validate injected/fixture services too; never infer status from the summary.
        orders = OrdersResponse.model_validate(response)
        truncated = scan.next_page == 50 and orders.hasMore
        has_more = orders.hasMore and not truncated
        records = []
        for order in response["orders"]:
            payload = {"order": order}
            if self.fetch_tracking:
                tracking = await self.service.track_order(account_id, order["orderNumber"])
                TrackingResponse.model_validate(tracking)
                if tracking["orderNumber"] != order["orderNumber"]:
                    raise ValueError("Oda tracking order number mismatch")
                payload["tracking"] = tracking
            digest = sha256(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            records.append(
                ProviderOrderRecord(
                    external_id=order["orderNumber"],
                    revision_id="oda-snapshot-v1:" + digest,
                    source_updated_at=None,
                    payload=payload,
                )
            )
        return ProviderPage(
            records=records,
            has_more=has_more,
            incomplete_reason=(
                "Oda history scan truncated at page 50; backfill is incomplete; "
                "available records captured and next poll restarts at page 1"
                if truncated
                else None
            ),
            next_cursor=_encode(
                scan.model_copy(
                    update={
                        "next_page": scan.next_page + 1 if has_more else 1,
                        "state": "incomplete"
                        if truncated
                        else "scanning"
                        if has_more
                        else "complete",
                    }
                )
            ),
        )
