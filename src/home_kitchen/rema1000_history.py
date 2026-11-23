"""Read-only normalization and adapter for Rema 1000 purchase history payloads.

Rema 1000 customer endpoints and the "Æ" loyalty program expose receipt lists and
detailed line-item digital receipts. This adapter normalizes receipt structures,
discounts (personal price cuts / "10 på 10" / "Æ-rabatt"), and line items without
calling live provider endpoints or loading real customer credentials.

Key constraints & boundaries:
- 24-month history limit: Rema 1000 Æ typically retains up to 24 months of purchases.
- Manual / app session authentication: Unattended OAuth refresh without user consent
  is not supported.
- Read-only: this adapter never modifies remote data and makes no assertions about
  current kitchen stock or recipe cooking completion.
- Zero household domain writes: receipts are captured into provider_order_sources;
  they are never written to kitchen_history or kitchen_inventory.
- Nullable EAN: line items preserve raw barcode / EAN as nullable strings without
  inferring canonical product identity.
- Fail-closed authentication: missing credentials or unauthenticated responses raise
  Rema1000AuthenticationError rather than treating history as empty.
"""

from __future__ import annotations

import hashlib
import json
from base64 import b64decode, urlsafe_b64encode
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID, uuid4

import httpx
from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from home_kitchen.purchase_sync import ProviderOrderRecord, ProviderPage, extract_line_ean
from home_kitchen.receipts import Receipt, ReceiptLine

REMA1000_SCHEMA_SOURCE = "https://ae.rema1000.no"
REMA1000_SCHEMA_VERSION = "v1"
REMA1000_HISTORY_WINDOW_MONTHS = 24
REMA1000_AUTH_METHOD = "manual_session_token"
DEFAULT_REMA1000_BASE_URL = "https://api.rema.no"
DEFAULT_REMA1000_SUBSCRIPTION_KEY = "fb5e24884b504d0bad761098f77e6605"
REMA1000_LIMITATIONS = (
    "Rema 1000 Æ retains up to 24 months of purchase history. "
    "Authentication requires a customer session token or app credential; "
    "unattended refresh without interactive login is not supported. "
    "Read-only adapter: zero writes to kitchen stock or confirmed purchases."
)


class Rema1000AuthenticationError(RuntimeError):
    """Missing or invalid Rema 1000 session credentials or unauthenticated response."""


class UnsupportedRema1000Contract(ValueError):
    """The observed response does not conform to the expected Rema 1000 contract."""


class Rema1000AdapterStatus(BaseModel):
    """Status metadata documenting provider capabilities and known limitations."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["rema1000"] = "rema1000"
    history_window_months: int = REMA1000_HISTORY_WINDOW_MONTHS
    auth_method: Literal["manual_session_token"] = REMA1000_AUTH_METHOD
    read_only: bool = True
    live_calls_enabled: bool = False
    limitations: str = REMA1000_LIMITATIONS


class Rema1000Discount(BaseModel):
    """Discount, price cut ('Æ-rabatt' / '10 på 10'), or campaign line."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    type: str | None = None
    description: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "description", "beskrivelse", "tittel", "navn", "name", "discountText"
        ),
        serialization_alias="description",
    )
    amount: Decimal = Field(
        default=Decimal("0"),
        validation_alias=AliasChoices(
            "amount", "belop", "rabatt", "priskutt", "aeDiscount", "rabattBelop"
        ),
        serialization_alias="amount",
    )
    code: str | None = None
    percentage: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices("percentage", "prosent", "rabattProsent"),
        serialization_alias="percentage",
    )


class Rema1000ReceiptLine(BaseModel):
    """Single item on a Rema 1000 receipt retaining nullable units and barcodes."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "name",
            "prodtxt1",
            "produktNavn",
            "produkt_navn",
            "beskrivelse",
            "vareTekst",
            "description",
            "item_name",
            "tekst",
        ),
        serialization_alias="name",
    )
    ean: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ean", "prodtxt3", "ean13", "gtin13", "gtin", "barcode"),
        serialization_alias="ean",
    )
    quantity: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices("quantity", "antall"),
        serialization_alias="quantity",
    )
    unit: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "unit", "enhet", "enhetsType", "enhets_type", "unit_type", "unitOfMeasure"
        ),
        serialization_alias="unit",
    )
    unit_price: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices("unitPrice", "unit_price", "enhetspris", "pris", "price"),
        serialization_alias="unitPrice",
    )
    amount: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "amount", "belop", "total", "linjesum", "linjebelop", "lineAmount"
        ),
        serialization_alias="amount",
    )
    discount: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "discount", "rabatt", "priskutt", "aeDiscount", "ae_rabatt", "personal_discount"
        ),
        serialization_alias="discount",
    )
    tax_percent: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "taxPercent", "tax_percent", "moms", "momsProsent", "vatPercent", "vat_percent"
        ),
        serialization_alias="taxPercent",
    )
    discounts: list[Rema1000Discount] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "discounts", "priskutt", "besparelser", "rabatter", "savings"
        ),
        serialization_alias="discounts",
    )

    @field_validator("ean", mode="before")
    @classmethod
    def normalize_ean(cls, value: object) -> str | None:
        if value is None or value == "$undefined" or value == "":
            return None
        s = str(value).strip()
        if not s or s == "$undefined":
            return None
        return s

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, value: object) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        if not s or s == "$undefined":
            return None
        return s

    def standardize(self) -> ReceiptLine:
        return ReceiptLine(
            name=self.name or "Unknown item",
            raw_name=self.name,
            quantity=self.quantity,
            unit=self.unit,
            amount=self.amount,
            ean=extract_line_ean({"ean": self.ean}),
            is_return=(self.quantity is not None and self.quantity < 0)
            or (self.amount is not None and self.amount < 0),
        )


class Rema1000Transaction(BaseModel):
    """Summary header from the Rema 1000 purchase or receipt list."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    receipt_id: str = Field(
        validation_alias=AliasChoices(
            "receiptId",
            "receipt_id",
            "id",
            "transactionId",
            "transaksjonsId",
            "kvitteringsId",
            "purchaseId",
        ),
        serialization_alias="receiptId",
    )
    store_name: str = Field(
        validation_alias=AliasChoices(
            "storeName", "store_name", "butikkNavn", "butikk", "store", "beskrivelse"
        ),
        serialization_alias="storeName",
    )
    store_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("storeId", "store_id", "butikknr", "storeNumber"),
        serialization_alias="storeId",
    )
    chain: str = Field(
        default="REMA 1000",
        validation_alias=AliasChoices("chain", "kjede"),
        serialization_alias="chain",
    )
    total_amount: Decimal = Field(
        validation_alias=AliasChoices(
            "totalAmount", "total_amount", "amount", "belop", "total", "sum"
        ),
        serialization_alias="totalAmount",
    )
    total_discount: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "totalDiscount", "total_discount", "discount", "rabatt", "priskutt", "aeCut", "ae_cut"
        ),
        serialization_alias="totalDiscount",
    )
    timestamp: AwareDatetime = Field(
        validation_alias=AliasChoices(
            "timestamp",
            "purchaseDate",
            "purchase_date",
            "tidspunkt",
            "dato",
            "transaksjonsTidspunkt",
        ),
        serialization_alias="timestamp",
    )
    has_receipt: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "hasReceipt", "has_receipt", "harKvittering", "has_digital_receipt"
        ),
        serialization_alias="hasReceipt",
    )

    @field_validator("receipt_id", mode="before")
    @classmethod
    def clean_receipt_id(cls, value: object) -> str:
        s = str(value or "").strip()
        if not s:
            raise ValueError("receipt_id must be non-empty")
        return s

    @field_validator("store_name", mode="before")
    @classmethod
    def clean_store_name(cls, value: object) -> str:
        s = str(value or "").strip()
        if not s:
            raise ValueError("store_name must be non-empty")
        return s


class Rema1000Receipt(BaseModel):
    """Detailed receipt retaining provider IDs, store attribution, and line items."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    receipt_id: str = Field(
        validation_alias=AliasChoices(
            "receiptId",
            "receipt_id",
            "id",
            "transactionId",
            "transaksjonsId",
            "kvitteringsId",
            "purchaseId",
        ),
        serialization_alias="receiptId",
    )
    store_name: str = Field(
        validation_alias=AliasChoices(
            "storeName", "store_name", "butikkNavn", "butikk", "store", "beskrivelse"
        ),
        serialization_alias="storeName",
    )
    store_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("storeId", "store_id", "butikknr", "storeNumber"),
        serialization_alias="storeId",
    )
    chain: str = Field(
        default="REMA 1000",
        validation_alias=AliasChoices("chain", "kjede"),
        serialization_alias="chain",
    )
    timestamp: AwareDatetime = Field(
        validation_alias=AliasChoices(
            "timestamp",
            "purchaseDate",
            "purchase_date",
            "tidspunkt",
            "dato",
            "transaksjonsTidspunkt",
        ),
        serialization_alias="timestamp",
    )
    total_amount: Decimal = Field(
        validation_alias=AliasChoices(
            "totalAmount", "total_amount", "amount", "belop", "total", "sum"
        ),
        serialization_alias="totalAmount",
    )
    total_discount: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "totalDiscount", "total_discount", "discount", "rabatt", "priskutt", "aeCut", "ae_cut"
        ),
        serialization_alias="totalDiscount",
    )
    has_receipt: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "hasReceipt", "has_receipt", "harKvittering", "has_digital_receipt"
        ),
        serialization_alias="hasReceipt",
    )
    lines: list[Rema1000ReceiptLine] = Field(
        default_factory=list,
        validation_alias=AliasChoices("lines", "rows", "varelinjer", "items", "receiptLines"),
        serialization_alias="lines",
    )
    source_uri: str = REMA1000_SCHEMA_SOURCE
    source_schema_version: str = REMA1000_SCHEMA_VERSION
    source_updated_at: None = None
    revision_id: str

    @field_validator("lines", mode="after")
    @classmethod
    def filter_non_item_lines(cls, value: list[Rema1000ReceiptLine]) -> list[Rema1000ReceiptLine]:
        # Skip non-item bonus/summary rows that lack both product name and barcode.
        return [line for line in value if line.name is not None or line.ean is not None]

    def standardize(self) -> Receipt:
        lines = [line.standardize() for line in self.lines]
        return Receipt(
            provider="rema1000",
            external_id=self.receipt_id,
            revision_id=self.revision_id,
            transaction_at=self.timestamp,
            store_name=self.store_name,
            store_id=self.store_id,
            chain=self.chain,
            total_amount=self.total_amount,
            total_discount=self.total_discount,
            has_receipt=self.has_receipt,
            lines=lines,
            source_uri=self.source_uri,
            source_schema_version=self.source_schema_version,
            source_updated_at=self.source_updated_at,
        )


def _revision_id(raw: dict[str, Any]) -> str:
    """Compute a deterministic hash of provider payload contents."""
    to_hash = {k: v for k, v in raw.items() if k != "revision_id"}
    encoded = json.dumps(
        to_hash,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_unauthenticated_dict(data: dict[str, Any]) -> bool:
    if data.get("authenticated") is False:
        return True
    status = data.get("status") or data.get("statusCode")
    if status in (401, 403):
        return True
    error = str(
        data.get("error") or data.get("message") or data.get("title") or data.get("detail") or ""
    ).casefold()
    if any(
        m in error
        for m in (
            "unauthorized",
            "unauthenticated",
            "forbidden",
            "logg inn",
            "innlogging",
            "login required",
        )
    ):
        return True
    return False


def _is_unauthenticated_str(text: str) -> bool:
    lower = text.casefold()
    if "unauthorized" in lower or "unauthenticated" in lower:
        return True
    if "<html" in lower or "<!doctype" in lower:
        if "logg inn" in lower or "login" in lower or "sign in" in lower:
            return True
    return False


def parse_transactions(
    payload: str | list[Any] | dict[str, Any],
) -> list[Rema1000Transaction]:
    """Parse Rema 1000 transaction summaries from JSON, dict, or list.

    Raises Rema1000AuthenticationError if the payload indicates an unauthenticated session.
    Raises UnsupportedRema1000Contract if the payload structure is not a supported list of transactions.
    """
    raw_list: Any
    if isinstance(payload, list):
        raw_list = payload
    elif isinstance(payload, dict):
        if _is_unauthenticated_dict(payload):
            raise Rema1000AuthenticationError(
                "Missing or invalid Rema 1000 credentials; unauthenticated response from provider"
            )
        if "receipts" in payload:
            raw_list = payload["receipts"]
        elif "transactions" in payload:
            raw_list = payload["transactions"]
        elif "heads" in payload:
            raw_list = payload["heads"]
        elif "purchases" in payload:
            raw_list = payload["purchases"]
        elif "data" in payload and isinstance(payload["data"], list):
            raw_list = payload["data"]
        elif "items" in payload and isinstance(payload["items"], list):
            raw_list = payload["items"]
        else:
            raise UnsupportedRema1000Contract(
                "Rema 1000 transaction response missing receipts/transactions list"
            )
    elif isinstance(payload, str):
        stripped = payload.strip()
        if _is_unauthenticated_str(stripped):
            raise Rema1000AuthenticationError(
                "Missing or invalid Rema 1000 credentials; unauthenticated response from provider"
            )
        try:
            parsed_json = json.loads(stripped)
            return parse_transactions(parsed_json)
        except json.JSONDecodeError as err:
            raise UnsupportedRema1000Contract(
                f"Malformed JSON in Rema 1000 payload: {err}"
            ) from err
    else:
        raise UnsupportedRema1000Contract(f"Unsupported payload type: {type(payload).__name__}")

    if not isinstance(raw_list, list):
        raise UnsupportedRema1000Contract("Rema 1000 transactions data must be an array")

    try:
        return [Rema1000Transaction.model_validate(item) for item in raw_list]
    except Exception as err:
        raise UnsupportedRema1000Contract(f"Invalid transaction in Rema 1000 list: {err}") from err


def parse_receipt(
    payload: str | dict[str, Any] | list[Any],
    *,
    transaction: Rema1000Transaction | None = None,
) -> Rema1000Receipt:
    """Normalize a Rema 1000 receipt detail payload from JSON or dict.

    Raises Rema1000AuthenticationError if the payload indicates an unauthenticated session.
    Raises UnsupportedRema1000Contract if the payload structure is not a supported receipt.
    """
    raw_dict: Any
    if isinstance(payload, str):
        stripped = payload.strip()
        if _is_unauthenticated_str(stripped):
            raise Rema1000AuthenticationError(
                "Missing or invalid Rema 1000 credentials; unauthenticated response from provider"
            )
        try:
            parsed_json = json.loads(stripped)
            return parse_receipt(parsed_json, transaction=transaction)
        except json.JSONDecodeError as err:
            raise UnsupportedRema1000Contract(
                f"Malformed JSON in Rema 1000 receipt: {err}"
            ) from err
    elif isinstance(payload, list) and transaction is not None:
        raw_dict = {"lines": payload}
    elif isinstance(payload, dict):
        if _is_unauthenticated_dict(payload):
            raise Rema1000AuthenticationError(
                "Missing or invalid Rema 1000 credentials; unauthenticated response from provider"
            )
        if "receipt" in payload and isinstance(payload["receipt"], dict):
            raw_dict = payload["receipt"]
        elif "receiptDetails" in payload and isinstance(payload["receiptDetails"], dict):
            raw_dict = payload["receiptDetails"]
        elif "purchase" in payload and isinstance(payload["purchase"], dict):
            raw_dict = payload["purchase"]
        elif "summary" in payload and isinstance(payload["summary"], dict):
            raw_dict = payload["summary"]
        elif "data" in payload and isinstance(payload["data"], dict):
            raw_dict = payload["data"]
        else:
            raw_dict = payload
    else:
        raise UnsupportedRema1000Contract(f"Unsupported payload type: {type(payload).__name__}")

    if not isinstance(raw_dict, dict):
        raise UnsupportedRema1000Contract("Rema 1000 receipt details must be an object")

    enriched = dict(raw_dict)
    if transaction is not None:
        if (
            "receiptId" not in enriched
            and "receipt_id" not in enriched
            and "id" not in enriched
            and "transactionId" not in enriched
        ):
            enriched["receiptId"] = transaction.receipt_id
        if (
            "storeName" not in enriched
            and "store_name" not in enriched
            and "butikkNavn" not in enriched
            and "store" not in enriched
            and "beskrivelse" not in enriched
        ):
            enriched["storeName"] = transaction.store_name
        if (
            "storeId" not in enriched
            and "store_id" not in enriched
            and "butikknr" not in enriched
            and transaction.store_id is not None
        ):
            enriched["storeId"] = transaction.store_id
        if "chain" not in enriched and "kjede" not in enriched:
            enriched["chain"] = transaction.chain
        if (
            "timestamp" not in enriched
            and "purchaseDate" not in enriched
            and "purchase_date" not in enriched
            and "tidspunkt" not in enriched
            and "dato" not in enriched
            and "transaksjonsTidspunkt" not in enriched
        ):
            enriched["timestamp"] = transaction.timestamp
        if (
            "totalAmount" not in enriched
            and "total_amount" not in enriched
            and "belop" not in enriched
            and "total" not in enriched
            and "amount" not in enriched
            and "sum" not in enriched
        ):
            enriched["totalAmount"] = transaction.total_amount
        if (
            "totalDiscount" not in enriched
            and "total_discount" not in enriched
            and "priskutt" not in enriched
            and "rabatt" not in enriched
            and "discount" not in enriched
            and "aeCut" not in enriched
            and transaction.total_discount is not None
        ):
            enriched["totalDiscount"] = transaction.total_discount
        if (
            "hasReceipt" not in enriched
            and "has_receipt" not in enriched
            and "harKvittering" not in enriched
        ):
            enriched["hasReceipt"] = transaction.has_receipt

    if "revision_id" not in enriched:
        enriched["revision_id"] = _revision_id(enriched)

    try:
        return Rema1000Receipt.model_validate(enriched)
    except Exception as err:
        raise UnsupportedRema1000Contract(f"Invalid Rema 1000 receipt structure: {err}") from err


def build_transaction_receipt_stub(transaction: Rema1000Transaction) -> Rema1000Receipt:
    """Build a provider record stub for a transaction that lacks receipt detail.

    This preserves provider transaction evidence while explicitly marking has_receipt=False
    and empty lines, preventing promotion to confirmed kitchen purchases.
    """
    raw: dict[str, Any] = {
        "receiptId": transaction.receipt_id,
        "storeName": transaction.store_name,
        "storeId": transaction.store_id,
        "chain": transaction.chain,
        "timestamp": transaction.timestamp,
        "totalAmount": transaction.total_amount,
        "totalDiscount": transaction.total_discount,
        "hasReceipt": False,
        "lines": [],
    }
    raw["revision_id"] = _revision_id(raw)
    return Rema1000Receipt.model_validate(raw)


def to_provider_order_record(receipt: Rema1000Receipt) -> ProviderOrderRecord:
    """Convert one normalized detail snapshot into the neutral immutable record.

    Rema 1000 payloads have no source update timestamp. The ingestion hash
    distinguishes changed detail snapshots, while observed_at is recorded by the
    neutral persistence layer.
    """
    external_id = receipt.standardize().external_id
    if not external_id or not external_id.strip():
        raise UnsupportedRema1000Contract("Rema 1000 receipt has no receipt_id")
    return ProviderOrderRecord(
        external_id=external_id.strip(),
        revision_id=receipt.revision_id,
        source_updated_at=None,
        payload=receipt.model_dump(mode="json", by_alias=True),
    )


class _Rema1000ScanCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    mode: Literal["full_rescan"]
    account_id: str
    scan_id: str
    next_page: int = Field(ge=1, le=50)
    state: Literal["scanning", "complete", "incomplete"]


def _encode_cursor(cursor: _Rema1000ScanCursor) -> str:
    return "rema1000-scan-v1." + urlsafe_b64encode(cursor.model_dump_json().encode()).decode()


def _decode_cursor(value: str) -> _Rema1000ScanCursor:
    try:
        prefix, encoded = value.split(".", 1)
        if prefix != "rema1000-scan-v1" or len(value) > 2048:
            raise ValueError
        cursor = _Rema1000ScanCursor.model_validate_json(
            b64decode(encoded, altchars=b"-_", validate=True)
        )
        UUID(cursor.scan_id)
        if cursor.state != "scanning" and cursor.next_page != 1:
            raise ValueError
        return cursor
    except (ValueError, TypeError):
        raise ValueError("Invalid Rema 1000 history scan cursor") from None


@runtime_checkable
class Rema1000Source(Protocol):
    """Protocol for reading Rema 1000 transactions and receipts without network coupling."""

    def get_transactions(self, account_id: str) -> list[Rema1000Transaction]: ...

    def get_receipt(self, account_id: str, receipt_id: str) -> Rema1000Receipt | None: ...


class Rema1000FixtureSource:
    """Fixture-backed offline source for Rema 1000 transactions and receipts."""

    def __init__(
        self,
        transactions: str | list[dict[str, Any]] | list[Rema1000Transaction] | dict[str, Any],
        receipts: dict[str, str | dict[str, Any] | Rema1000Receipt] | None = None,
        *,
        failure: Exception | None = None,
    ) -> None:
        self._failure = failure
        if isinstance(transactions, list):
            self._transactions = [
                t if isinstance(t, Rema1000Transaction) else Rema1000Transaction.model_validate(t)
                for t in transactions
            ]
        else:
            self._transactions = parse_transactions(transactions)

        self._receipts: dict[str, str | dict[str, Any] | Rema1000Receipt] = receipts or {}
        self.transaction_calls = 0
        self.receipt_calls: list[str] = []

    def get_transactions(self, account_id: str) -> list[Rema1000Transaction]:
        self.transaction_calls += 1
        if self._failure is not None:
            raise self._failure
        return list(self._transactions)

    def get_receipt(self, account_id: str, receipt_id: str) -> Rema1000Receipt | None:
        self.receipt_calls.append(receipt_id)
        if self._failure is not None:
            raise self._failure
        raw = self._receipts.get(receipt_id)
        if raw is None:
            return None
        if isinstance(raw, Rema1000Receipt):
            return raw
        tx = next((t for t in self._transactions if t.receipt_id == receipt_id), None)
        return parse_receipt(raw, transaction=tx)


class Rema1000HttpSource:
    """Live HTTP source for Rema 1000 Æ purchases and receipts."""

    def __init__(
        self,
        session_token: str,
        *,
        base_url: str = DEFAULT_REMA1000_BASE_URL,
        subscription_key: str = DEFAULT_REMA1000_SUBSCRIPTION_KEY,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        clean_token = session_token.strip() if isinstance(session_token, str) else ""
        if not clean_token:
            raise Rema1000AuthenticationError("Rema 1000-sesjonstoken mangler.")
        clean_key = subscription_key.strip() if isinstance(subscription_key, str) else ""
        self.session_token = clean_token
        self.base_url = base_url.rstrip("/")
        self.subscription_key = clean_key or DEFAULT_REMA1000_SUBSCRIPTION_KEY
        self.transport = transport
        self._tx_cache: dict[str, Rema1000Transaction] = {}

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=self.transport,
            timeout=25.0,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {self.session_token}",
                "Accept": "application/json",
                "Ocp-Apim-Subscription-Key": self.subscription_key,
                "X-App": "Bella",
                "X-Platform": "CLI",
                "X-App-Version": "3.13.0.1181",
                "Accept-Language": "no",
                "User-Agent": "HomeKitchen/1.0",
            },
        )

    def get_transactions(self, account_id: str) -> list[Rema1000Transaction]:
        with self._client() as client:
            resp = client.get(f"{self.base_url}/v1/bella/transaction/v2/heads")
            if resp.status_code == 404:
                resp = client.get(f"{self.base_url}/v1/receipts")
        if resp.status_code in (401, 403):
            raise Rema1000AuthenticationError(
                "Rema 1000-sesjonen er utløpt eller ugyldig; forny eller koble til på nytt i Innstillinger."
            )
        if resp.status_code >= 400:
            raise UnsupportedRema1000Contract(f"Rema 1000 svarte med HTTP {resp.status_code}")
        txs = parse_transactions(resp.text)
        for tx in txs:
            self._tx_cache[tx.receipt_id] = tx
        return txs

    def get_receipt(self, account_id: str, receipt_id: str) -> Rema1000Receipt | None:
        url = f"{self.base_url}/v1/bella/transaction/v2/rows/{receipt_id}"
        with self._client() as client:
            resp = client.get(url)
        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise Rema1000AuthenticationError(
                "Rema 1000-sesjonen er utløpt eller ugyldig; forny eller koble til på nytt i Innstillinger."
            )
        if resp.status_code >= 400:
            raise UnsupportedRema1000Contract(
                f"Rema 1000 kvittering {receipt_id} svarte med HTTP {resp.status_code}"
            )
        return parse_receipt(resp.text, transaction=self._tx_cache.get(receipt_id))


class Rema1000PurchaseHistoryAdapter:
    """Read-only adapter for Rema 1000 receipt and purchase history.

    Bound to one local actor's Rema 1000 connection; does not perform any household writes
    or stock assertions. Uses bounded full-rescan cursors with max 50 pages and
    idempotent deduplication via payload revision hashes.
    """

    def __init__(
        self,
        source: Rema1000Source,
        account_id: str,
        *,
        page_size: int = 20,
        max_pages: int = 50,
        include_non_receipt_transactions: bool = True,
    ) -> None:
        trimmed = account_id.strip()
        if not trimmed or trimmed != account_id:
            raise ValueError("Rema 1000 account_id must be non-empty and trimmed")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        if max_pages < 1 or max_pages > 50:
            raise ValueError("max_pages must be between 1 and 50")

        self.source = source
        self.account_id = account_id
        self.page_size = page_size
        self.max_pages = max_pages
        self.include_non_receipt_transactions = include_non_receipt_transactions

    def status(self) -> Rema1000AdapterStatus:
        """Return provider status metadata and limitations."""
        return Rema1000AdapterStatus()

    def fetch_page(self, account_id: str, cursor: str | None) -> ProviderPage:
        """Fetch one bounded page of normalized Rema 1000 order records."""
        if account_id != self.account_id:
            raise ValueError("Rema 1000 history adapter account mismatch")

        scan = _decode_cursor(cursor) if cursor is not None else None
        if scan is not None and scan.account_id != account_id:
            raise ValueError("Rema 1000 history cursor account mismatch")

        if scan is None or scan.state != "scanning":
            scan = _Rema1000ScanCursor(
                version=1,
                mode="full_rescan",
                account_id=account_id,
                scan_id=str(uuid4()),
                next_page=1,
                state="scanning",
            )

        transactions = self.source.get_transactions(account_id)

        page_num = scan.next_page
        start_idx = (page_num - 1) * self.page_size
        end_idx = start_idx + self.page_size
        page_transactions = transactions[start_idx:end_idx]

        has_more_records = end_idx < len(transactions)
        truncated = page_num == self.max_pages and has_more_records
        has_more = has_more_records and not truncated

        records: list[ProviderOrderRecord] = []
        for tx in page_transactions:
            if tx.has_receipt:
                receipt = self.source.get_receipt(account_id, tx.receipt_id)
                if receipt is not None:
                    records.append(to_provider_order_record(receipt))
                elif self.include_non_receipt_transactions:
                    stub = build_transaction_receipt_stub(tx)
                    records.append(to_provider_order_record(stub))
            elif self.include_non_receipt_transactions:
                stub = build_transaction_receipt_stub(tx)
                records.append(to_provider_order_record(stub))

        next_cursor = _encode_cursor(
            scan.model_copy(
                update={
                    "next_page": page_num + 1 if has_more else 1,
                    "state": (
                        "incomplete" if truncated else "scanning" if has_more else "complete"
                    ),
                }
            )
        )

        incomplete_reason = (
            f"Rema 1000 history scan truncated at page {self.max_pages}; backfill is incomplete; "
            f"available records captured and next poll restarts at page 1"
            if truncated
            else None
        )

        return ProviderPage(
            records=records,
            next_cursor=next_cursor,
            has_more=has_more,
            incomplete_reason=incomplete_reason,
        )
