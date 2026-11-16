"""Read-only normalization and adapter for Trumf purchase history payloads.

Trumf web endpoints (www.trumf.no/trumf-profil/kvitteringer) stream Next.js App Router
RSC flight payloads. This adapter extracts transactions and detailed receipt line items
without calling live provider endpoints or loading real household credentials.

Key constraints & boundaries:
- 12-month history limit: Trumf exposes at most the last 12 months of transactions.
- Manual cookie authentication: Trumf web sessions currently require a browser session
  cookie; unattended OAuth refresh is not supported.
- Read-only: this adapter never modifies remote data and makes no assertions about
  current kitchen stock or recipe cooking completion.
- Receipt presence: transactions with harKvittering=False retain provider presence
  as source evidence but must never be promoted to confirmed kitchen purchases.
"""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, urlsafe_b64encode
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from home_kitchen.purchase_sync import ProviderOrderRecord, ProviderPage

TRUMF_SCHEMA_SOURCE = "https://www.trumf.no/trumf-profil/kvitteringer"
TRUMF_SCHEMA_VERSION = "rsc-flight-v1"
TRUMF_PARSER_REFERENCE = "https://github.com/fredrsat/trumf-connector"
TRUMF_HISTORY_WINDOW_MONTHS = 12
TRUMF_AUTH_METHOD = "manual_session_cookie"
TRUMF_LIMITATIONS = (
    "Trumf retains at most the last 12 months of purchase history. "
    "Authentication requires a manual browser session cookie with periodic re-authentication; "
    "unattended refresh is not currently supported by Trumf's web endpoints."
)


class TrumfAuthenticationError(RuntimeError):
    """Missing or invalid Trumf session payload; no flight data found in page."""


class UnsupportedTrumfContract(ValueError):
    """The observed response does not conform to the expected Trumf contract."""


class TrumfAdapterStatus(BaseModel):
    """Status metadata documenting provider capabilities and known limitations."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["trumf"] = "trumf"
    history_window_months: int = TRUMF_HISTORY_WINDOW_MONTHS
    auth_method: Literal["manual_session_cookie"] = "manual_session_cookie"
    read_only: bool = True
    live_calls_enabled: bool = False
    limitations: str = TRUMF_LIMITATIONS


class TrumfSaving(BaseModel):
    """Discount, offer, or campaign line associated with a purchased product."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    type: str | None = None
    beskrivelse: str | None = Field(
        default=None,
        validation_alias=AliasChoices("beskrivelse", "description"),
        serialization_alias="beskrivelse",
    )
    belop: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices("belop", "amount"),
        serialization_alias="belop",
    )


class TrumfReceiptLine(BaseModel):
    """Single item on a Trumf receipt retaining nullable source units and unknown barcodes."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    produkt_beskrivelse: str = Field(
        validation_alias=AliasChoices(
            "produktBeskrivelse", "vareTekst", "produkt_beskrivelse", "name"
        ),
        serialization_alias="produktBeskrivelse",
    )
    ean: str | None = None
    antall: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices("antall", "quantity"),
        serialization_alias="antall",
    )
    belop: Decimal = Field(
        validation_alias=AliasChoices("belop", "total", "amount"),
        serialization_alias="belop",
    )
    enhets_type: str | None = Field(
        default=None,
        validation_alias=AliasChoices("enhetsType", "enhets_type", "unit", "unit_type"),
        serialization_alias="enhetsType",
    )
    moms_prosent: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices("momsProsent", "moms_prosent", "vatPercent", "vat_percent"),
        serialization_alias="momsProsent",
    )
    bonus: Decimal | None = None
    besparelser: list[TrumfSaving] = Field(
        default_factory=list,
        validation_alias=AliasChoices("besparelser", "savings", "discounts"),
        serialization_alias="besparelser",
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

    @field_validator("produkt_beskrivelse", mode="before")
    @classmethod
    def clean_name(cls, value: object) -> str:
        s = str(value or "").strip()
        if not s:
            raise ValueError("produktBeskrivelse must be non-empty")
        return s


class TrumfTransaction(BaseModel):
    """Summary header from the Trumf transaction list."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    batch_id: str = Field(
        validation_alias=AliasChoices("batchId", "batchid", "batch_id"),
        serialization_alias="batchId",
    )
    beskrivelse: str = Field(
        validation_alias=AliasChoices("beskrivelse", "storeName", "store_name", "store"),
        serialization_alias="beskrivelse",
    )
    belop: Decimal = Field(
        validation_alias=AliasChoices("belop", "amount", "total"),
        serialization_alias="belop",
    )
    bonus: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices("bonus", "trumftotal", "trumf"),
        serialization_alias="bonus",
    )
    transaksjons_tidspunkt: AwareDatetime = Field(
        validation_alias=AliasChoices(
            "transaksjonsTidspunkt", "dato", "transaksjons_tidspunkt", "timestamp"
        ),
        serialization_alias="transaksjonsTidspunkt",
    )
    filter_category: str | None = Field(
        default=None,
        validation_alias=AliasChoices("filterCategory", "kjedeid", "filter_category", "chain"),
        serialization_alias="filterCategory",
    )
    partner_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("partnerId", "partnerid", "partner_id"),
        serialization_alias="partnerId",
    )
    har_kvittering: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "harKvittering", "har_kvittering", "hasReceipt", "has_receipt"
        ),
        serialization_alias="harKvittering",
    )
    medlem_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("medlemId", "medlem_id"),
        serialization_alias="medlemId",
    )
    transaksjon_kategori: str | None = Field(
        default=None,
        validation_alias=AliasChoices("transaksjonKategori", "transaksjon_kategori"),
        serialization_alias="transaksjonKategori",
    )


def chain_of(transaction: TrumfTransaction) -> str | None:
    """Infer chain identifier: filterCategory > partnerId > first word of store description."""
    if transaction.filter_category:
        return transaction.filter_category
    if transaction.partner_id:
        return transaction.partner_id
    if transaction.beskrivelse:
        first_word = transaction.beskrivelse.split()[0]
        return first_word if first_word else None
    return None


class TrumfReceipt(BaseModel):
    """Detailed receipt retaining provider IDs, store attribution, and line items."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    batch_id: str = Field(
        validation_alias=AliasChoices("batchId", "batchid", "batch_id"),
        serialization_alias="batchId",
    )
    kvitterings_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("kvitteringsId", "kvitterings_id", "receiptId", "receipt_id"),
        serialization_alias="kvitteringsId",
    )
    belop: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices("belop", "amount", "total"),
        serialization_alias="belop",
    )
    bonus: Decimal | None = Field(default=None)
    transaksjons_tidspunkt: AwareDatetime | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "transaksjonsTidspunkt", "dato", "transaksjons_tidspunkt", "purchaseDate"
        ),
        serialization_alias="transaksjonsTidspunkt",
    )
    store_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("storeName", "store_name", "beskrivelse"),
        serialization_alias="storeName",
    )
    chain: str | None = Field(
        default=None,
        validation_alias=AliasChoices("chain", "filterCategory", "kjedeid"),
        serialization_alias="chain",
    )
    has_receipt: bool = Field(
        default=True,
        validation_alias=AliasChoices("hasReceipt", "has_receipt", "harKvittering"),
        serialization_alias="hasReceipt",
    )
    varelinjer: list[TrumfReceiptLine] = Field(
        default_factory=list,
        validation_alias=AliasChoices("varelinjer", "lines"),
        serialization_alias="varelinjer",
    )
    source_uri: str = TRUMF_SCHEMA_SOURCE
    source_schema_version: str = TRUMF_SCHEMA_VERSION
    source_updated_at: None = None
    revision_id: str


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


def extract_rsc_flight_json(html: str, key: str) -> Any | None:
    """Extract and parse a JSON array or object following key in an RSC flight payload.

    Next.js App Router RSC streams flight data inside script tags with escaped quotes (\\").
    This parser searches for the key marker, performs string-aware bracket matching,
    unescapes quotes and backslashes, strips the RSC '$D' date prefix, and parses JSON.
    Returns None if the key marker or structure is not found.
    """
    marker = key + '\\":'
    at = html.find(marker)
    if at < 0:
        marker = key + '":'
        at = html.find(marker)
    if at < 0:
        return None

    i = at + len(marker)
    while i < len(html) and html[i] not in "[{":
        i += 1
    if i >= len(html):
        return None

    open_c = html[i]
    close_c = "]" if open_c == "[" else "}"
    depth = 0
    in_str = False
    j = i
    while j < len(html):
        c = html[j]
        if in_str:
            if c == "\\" and j + 1 < len(html) and html[j + 1] == '"':
                b = 0
                k = j
                while k >= 0 and html[k] == "\\":
                    b += 1
                    k -= 1
                if b % 2 == 1:
                    in_str = False
                j += 2
                continue
            elif c == '"':
                in_str = False
        else:
            if c == "\\" and j + 1 < len(html) and html[j + 1] == '"':
                in_str = True
                j += 2
                continue
            elif c == '"':
                in_str = True
            elif c == open_c:
                depth += 1
            elif c == close_c:
                depth -= 1
                if depth == 0:
                    j += 1
                    break
        j += 1

    if depth != 0:
        return None

    raw = html[i:j]
    unescaped = raw.replace('\\"', '"').replace("\\\\", "\\")
    unescaped = re.sub(r'"\$D', '"', unescaped)
    try:
        return json.loads(unescaped)
    except json.JSONDecodeError as err:
        raise UnsupportedTrumfContract(f"Malformed JSON in RSC payload: {err}") from err


def parse_transactions(
    payload: str | list[Any] | dict[str, Any],
) -> list[TrumfTransaction]:
    """Parse Trumf transaction summaries from RSC flight HTML, dict, or list.

    Raises TrumfAuthenticationError if RSC flight payload is missing or unauthenticated.
    Raises UnsupportedTrumfContract if the payload structure is not a supported list of transactions.
    """
    raw_list: Any
    if isinstance(payload, list):
        raw_list = payload
    elif isinstance(payload, dict):
        if "transactionsData" in payload:
            raw_list = payload["transactionsData"]
        elif "purchases" in payload:
            raw_list = payload["purchases"]
        elif "transactions" in payload:
            raw_list = payload["transactions"]
        else:
            raise UnsupportedTrumfContract("Trumf transaction response missing transactionsData")
    elif isinstance(payload, str):
        stripped = payload.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            try:
                parsed_json = json.loads(stripped)
                return parse_transactions(parsed_json)
            except json.JSONDecodeError:
                pass
        extracted = extract_rsc_flight_json(payload, "transactionsData")
        if extracted is None:
            raise TrumfAuthenticationError(
                "Missing or invalid Trumf session payload; no transaction data found in page"
            )
        raw_list = extracted
    else:
        raise UnsupportedTrumfContract(f"Unsupported payload type: {type(payload).__name__}")

    if not isinstance(raw_list, list):
        raise UnsupportedTrumfContract("Trumf transactions data must be an array")

    return [TrumfTransaction.model_validate(item) for item in raw_list]


def parse_receipt(
    payload: str | dict[str, Any],
    *,
    transaction: TrumfTransaction | None = None,
) -> TrumfReceipt:
    """Normalize a Trumf receipt detail payload from RSC flight HTML or dict.

    Raises TrumfAuthenticationError if RSC flight payload is missing or unauthenticated.
    Raises UnsupportedTrumfContract if the payload structure is not a supported receipt.
    """
    raw_dict: Any
    if isinstance(payload, dict):
        if "purchaseDetails" in payload:
            raw_dict = payload["purchaseDetails"]
        elif "summary" in payload:
            raw_dict = payload["summary"]
        else:
            raw_dict = payload
    elif isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed_json = json.loads(stripped)
                return parse_receipt(parsed_json, transaction=transaction)
            except json.JSONDecodeError:
                pass
        extracted = extract_rsc_flight_json(payload, "purchaseDetails")
        if extracted is None:
            raise TrumfAuthenticationError(
                "Missing or invalid Trumf session payload; no receipt details found in page"
            )
        raw_dict = extracted
    else:
        raise UnsupportedTrumfContract(f"Unsupported payload type: {type(payload).__name__}")

    if not isinstance(raw_dict, dict):
        raise UnsupportedTrumfContract("Trumf purchase details must be an object")

    enriched = dict(raw_dict)
    if transaction is not None:
        if "batchId" not in enriched and "batchid" not in enriched:
            enriched["batchId"] = transaction.batch_id
        if (
            "storeName" not in enriched
            and "beskrivelse" not in enriched
            and transaction.beskrivelse
        ):
            enriched["storeName"] = transaction.beskrivelse
        if (
            "chain" not in enriched
            and "filterCategory" not in enriched
            and "kjedeid" not in enriched
        ):
            chain = chain_of(transaction)
            if chain:
                enriched["chain"] = chain
        if "transaksjonsTidspunkt" not in enriched and "dato" not in enriched:
            enriched["transaksjonsTidspunkt"] = transaction.transaksjons_tidspunkt
        if "belop" not in enriched:
            enriched["belop"] = transaction.belop
        if "bonus" not in enriched and transaction.bonus is not None:
            enriched["bonus"] = transaction.bonus

    if "revision_id" not in enriched:
        enriched["revision_id"] = _revision_id(enriched)

    return TrumfReceipt.model_validate(enriched)


def build_transaction_receipt_stub(transaction: TrumfTransaction) -> TrumfReceipt:
    """Build a provider record stub for a transaction that lacks receipt detail.

    This preserves provider transaction evidence while explicitly marking has_receipt=False
    and empty varelinjer, preventing promotion to confirmed kitchen purchases.
    """
    raw: dict[str, Any] = {
        "batchId": transaction.batch_id,
        "storeName": transaction.beskrivelse,
        "chain": chain_of(transaction),
        "transaksjonsTidspunkt": transaction.transaksjons_tidspunkt,
        "belop": transaction.belop,
        "bonus": transaction.bonus,
        "hasReceipt": False,
        "varelinjer": [],
    }
    raw["revision_id"] = _revision_id(raw)
    return TrumfReceipt.model_validate(raw)


def to_provider_order_record(receipt: TrumfReceipt) -> ProviderOrderRecord:
    """Convert one normalized detail snapshot into the neutral immutable record.

    Trumf payloads have no source update timestamp. The ingestion hash
    distinguishes changed detail snapshots, while observed_at is recorded by the
    neutral persistence layer.
    """
    external_id = receipt.batch_id
    if not external_id or not external_id.strip():
        raise UnsupportedTrumfContract("Trumf receipt has no batchId")
    return ProviderOrderRecord(
        external_id=external_id.strip(),
        revision_id=receipt.revision_id,
        source_updated_at=None,
        payload=receipt.model_dump(mode="json", by_alias=True),
    )


class _TrumfScanCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    mode: Literal["full_rescan"]
    account_id: str
    scan_id: str
    next_page: int = Field(ge=1, le=50)
    state: Literal["scanning", "complete", "incomplete"]


def _encode_cursor(cursor: _TrumfScanCursor) -> str:
    return "trumf-scan-v1." + urlsafe_b64encode(cursor.model_dump_json().encode()).decode()


def _decode_cursor(value: str) -> _TrumfScanCursor:
    try:
        prefix, encoded = value.split(".", 1)
        if prefix != "trumf-scan-v1" or len(value) > 2048:
            raise ValueError
        cursor = _TrumfScanCursor.model_validate_json(
            b64decode(encoded, altchars=b"-_", validate=True)
        )
        UUID(cursor.scan_id)
        if cursor.state != "scanning" and cursor.next_page != 1:
            raise ValueError
        return cursor
    except (ValueError, TypeError):
        raise ValueError("Invalid Trumf history scan cursor") from None


@runtime_checkable
class TrumfSource(Protocol):
    """Protocol for reading Trumf transactions and receipts without network coupling."""

    def get_transactions(self, account_id: str) -> list[TrumfTransaction]: ...

    def get_receipt(self, account_id: str, batch_id: str) -> TrumfReceipt | None: ...


class TrumfFixtureSource:
    """Fixture-backed offline source for Trumf transactions and receipts."""

    def __init__(
        self,
        transactions: str | list[dict[str, Any]] | list[TrumfTransaction],
        receipts: dict[str, str | dict[str, Any] | TrumfReceipt] | None = None,
        *,
        failure: Exception | None = None,
    ) -> None:
        self._failure = failure
        if isinstance(transactions, list):
            self._transactions = [
                t if isinstance(t, TrumfTransaction) else TrumfTransaction.model_validate(t)
                for t in transactions
            ]
        else:
            self._transactions = parse_transactions(transactions)

        self._receipts: dict[str, str | dict[str, Any] | TrumfReceipt] = receipts or {}
        self.transaction_calls = 0
        self.receipt_calls: list[str] = []

    def get_transactions(self, account_id: str) -> list[TrumfTransaction]:
        self.transaction_calls += 1
        if self._failure is not None:
            raise self._failure
        return list(self._transactions)

    def get_receipt(self, account_id: str, batch_id: str) -> TrumfReceipt | None:
        self.receipt_calls.append(batch_id)
        if self._failure is not None:
            raise self._failure
        raw = self._receipts.get(batch_id)
        if raw is None:
            return None
        if isinstance(raw, TrumfReceipt):
            return raw
        tx = next((t for t in self._transactions if t.batch_id == batch_id), None)
        return parse_receipt(raw, transaction=tx)


class TrumfPurchaseHistoryAdapter:
    """Read-only adapter for Trumf receipt and purchase history.

    Bound to one local actor's Trumf connection; does not perform any household writes
    or stock assertions. Uses bounded full-rescan cursors with max 50 pages and
    idempotent deduplication via payload revision hashes.
    """

    def __init__(
        self,
        source: TrumfSource,
        account_id: str,
        *,
        page_size: int = 20,
        max_pages: int = 50,
        include_non_receipt_transactions: bool = True,
    ) -> None:
        trimmed = account_id.strip()
        if not trimmed or trimmed != account_id:
            raise ValueError("Trumf account_id must be non-empty and trimmed")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        if max_pages < 1 or max_pages > 50:
            raise ValueError("max_pages must be between 1 and 50")

        self.source = source
        self.account_id = account_id
        self.page_size = page_size
        self.max_pages = max_pages
        self.include_non_receipt_transactions = include_non_receipt_transactions

    def status(self) -> TrumfAdapterStatus:
        """Return provider status metadata and limitations."""
        return TrumfAdapterStatus()

    def fetch_page(self, account_id: str, cursor: str | None) -> ProviderPage:
        """Fetch one bounded page of normalized Trumf order records."""
        if account_id != self.account_id:
            raise ValueError("Trumf history adapter account mismatch")

        scan = _decode_cursor(cursor) if cursor is not None else None
        if scan is not None and scan.account_id != account_id:
            raise ValueError("Trumf history cursor account mismatch")

        if scan is None or scan.state != "scanning":
            scan = _TrumfScanCursor(
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
            if tx.har_kvittering:
                receipt = self.source.get_receipt(account_id, tx.batch_id)
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
            f"Trumf history scan truncated at page {self.max_pages}; backfill is incomplete; "
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
