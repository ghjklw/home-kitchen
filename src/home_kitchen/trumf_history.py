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
import urllib.parse
from base64 import b64decode, urlsafe_b64encode
from datetime import UTC, date, datetime
from datetime import time as dt_time
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
    ValidationError,
    field_validator,
)

from home_kitchen.purchase_sync import ProviderOrderRecord, ProviderPage, extract_line_ean
from home_kitchen.receipts import Receipt, ReceiptLine

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


_COOKIE_ATTR_FLAGS = frozenset({"httponly", "secure", "partitioned"})
_COOKIE_ATTR_NAMES = frozenset({"path", "domain", "expires", "max-age", "samesite", "priority"})


def _is_missing_rsc_value(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip() in ("", "$undefined")


def _clean_optional_str(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "$undefined":
        return None
    return s


def _clean_optional_decimal(value: object) -> object:
    if _is_missing_rsc_value(value):
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid decimal value")
    return value


def _clean_required_decimal(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid decimal value")
    if isinstance(value, str) and value.strip() == "$undefined":
        raise ValueError("Missing decimal value ($undefined)")
    return value


def _validate_finite_decimal(value: Decimal | None) -> Decimal | None:
    if value is not None and not value.is_finite():
        raise ValueError("Decimal value must be finite")
    return value


def _normalize_timestamp(value: object) -> object:
    if _is_missing_rsc_value(value):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("$D"):
            s = s[2:].strip()
        if not s or s == "$undefined":
            return None
        if len(s) == 10 and s.count("-") == 2:
            return datetime.combine(date.fromisoformat(s), dt_time.min, tzinfo=UTC)
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    return value


def _coerce_optional_list(value: object) -> object:
    if _is_missing_rsc_value(value):
        return []
    return value


def _coerce_bool_with_default(value: object, *, default: bool) -> object:
    if _is_missing_rsc_value(value):
        return default
    return value


def _normalize_cookie_header(raw_cookie: str) -> str:
    """Strip Cookie/Set-Cookie prefixes, newlines, and attributes for HTTP requests."""
    cookies: dict[str, str] = {}
    for segment in re.split(r"[\r\n;]+", raw_cookie):
        cleaned = re.sub(r"^(?:cookie|set-cookie):\s*", "", segment.strip(), flags=re.IGNORECASE)
        if not cleaned or cleaned.lower() in _COOKIE_ATTR_FLAGS:
            continue
        if "=" not in cleaned:
            continue
        name, val = cleaned.split("=", 1)
        name = name.strip()
        val = val.strip()
        if not name or name.lower() in _COOKIE_ATTR_NAMES:
            continue
        cookies[name] = val
    if cookies:
        return "; ".join(f"{k}={v}" for k, v in cookies.items())
    stripped = re.sub(r"[\r\n]+", " ", raw_cookie).strip()
    if stripped.lower() in _COOKIE_ATTR_FLAGS:
        return ""
    if "=" in stripped:
        attr_name = stripped.split("=", 1)[0].strip().lower()
        if attr_name in _COOKIE_ATTR_NAMES:
            return ""
    return stripped


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

    @field_validator("type", "beskrivelse", mode="before")
    @classmethod
    def clean_optional_text(cls, value: object) -> str | None:
        return _clean_optional_str(value)

    @field_validator("belop", mode="before")
    @classmethod
    def clean_belop(cls, value: object) -> object:
        return _clean_optional_decimal(value)

    @field_validator("belop", mode="after")
    @classmethod
    def check_finite_belop(cls, value: Decimal | None) -> Decimal | None:
        return _validate_finite_decimal(value)


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
    belop: Decimal | None = Field(
        default=None,
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

    @field_validator("ean", "enhets_type", mode="before")
    @classmethod
    def normalize_optional_str(cls, value: object) -> str | None:
        return _clean_optional_str(value)

    @field_validator("produkt_beskrivelse", mode="before")
    @classmethod
    def clean_name(cls, value: object) -> str:
        s = _clean_optional_str(value)
        if not s:
            raise ValueError("produktBeskrivelse must be non-empty")
        return s

    @field_validator("antall", "belop", "moms_prosent", "bonus", mode="before")
    @classmethod
    def clean_decimals(cls, value: object) -> object:
        return _clean_optional_decimal(value)

    @field_validator("antall", "belop", "moms_prosent", "bonus", mode="after")
    @classmethod
    def check_finite_decimals(cls, value: Decimal | None) -> Decimal | None:
        return _validate_finite_decimal(value)

    @field_validator("besparelser", mode="before")
    @classmethod
    def clean_besparelser(cls, value: object) -> object:
        return _coerce_optional_list(value)

    def standardize(self) -> ReceiptLine:
        return ReceiptLine(
            name=self.produkt_beskrivelse,
            raw_name=self.produkt_beskrivelse,
            quantity=self.antall,
            unit=self.enhets_type,
            amount=self.belop,
            ean=extract_line_ean({"ean": self.ean}),
            is_return=(self.antall is not None and self.antall < 0)
            or (self.belop is not None and self.belop < 0),
        )


class TrumfTransaction(BaseModel):
    """Summary header from the Trumf transaction list."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    batch_id: str | None = Field(
        default=None,
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

    @field_validator(
        "batch_id",
        "filter_category",
        "partner_id",
        "medlem_id",
        "transaksjon_kategori",
        mode="before",
    )
    @classmethod
    def clean_optional_fields(cls, value: object) -> str | None:
        return _clean_optional_str(value)

    @field_validator("beskrivelse", mode="before")
    @classmethod
    def clean_beskrivelse(cls, value: object) -> str:
        s = _clean_optional_str(value)
        if not s:
            raise ValueError("beskrivelse must be non-empty")
        return s

    @field_validator("belop", mode="before")
    @classmethod
    def clean_belop(cls, value: object) -> object:
        return _clean_required_decimal(value)

    @field_validator("bonus", mode="before")
    @classmethod
    def clean_bonus(cls, value: object) -> object:
        return _clean_optional_decimal(value)

    @field_validator("belop", "bonus", mode="after")
    @classmethod
    def check_finite_decimals(cls, value: Decimal | None) -> Decimal | None:
        return _validate_finite_decimal(value)

    @field_validator("transaksjons_tidspunkt", mode="before")
    @classmethod
    def clean_timestamp(cls, value: object) -> object:
        return _normalize_timestamp(value)

    @field_validator("har_kvittering", mode="before")
    @classmethod
    def clean_har_kvittering(cls, value: object) -> object:
        return _coerce_bool_with_default(value, default=True)


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


def _has_line_identity(item: object) -> bool:
    if isinstance(item, TrumfReceiptLine):
        return bool(_clean_optional_str(item.produkt_beskrivelse))
    if not isinstance(item, dict):
        return True
    for key in ("produktBeskrivelse", "vareTekst", "produkt_beskrivelse", "name"):
        if _clean_optional_str(item.get(key)) is not None:
            return True
    return False


class TrumfReceipt(BaseModel):
    """Detailed receipt retaining provider IDs, store attribution, and line items."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    batch_id: str | None = Field(
        default=None,
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

    @field_validator("batch_id", "kvitterings_id", "store_name", "chain", mode="before")
    @classmethod
    def clean_optional_fields(cls, value: object) -> str | None:
        return _clean_optional_str(value)

    @field_validator("belop", "bonus", mode="before")
    @classmethod
    def clean_decimals(cls, value: object) -> object:
        return _clean_optional_decimal(value)

    @field_validator("belop", "bonus", mode="after")
    @classmethod
    def check_finite_decimals(cls, value: Decimal | None) -> Decimal | None:
        return _validate_finite_decimal(value)

    @field_validator("transaksjons_tidspunkt", mode="before")
    @classmethod
    def clean_timestamp(cls, value: object) -> object:
        return _normalize_timestamp(value)

    @field_validator("has_receipt", mode="before")
    @classmethod
    def clean_has_receipt(cls, value: object) -> object:
        return _coerce_bool_with_default(value, default=True)

    @field_validator("varelinjer", mode="before")
    @classmethod
    def clean_varelinjer(cls, value: object) -> object:
        coerced = _coerce_optional_list(value)
        if isinstance(coerced, list):
            # Skip non-item discount/summary rows lacking a product description.
            return [item for item in coerced if _has_line_identity(item)]
        return coerced

    def standardize(self) -> Receipt:
        lines = [line.standardize() for line in self.varelinjer]
        return Receipt(
            provider="trumf",
            external_id=self.batch_id,
            revision_id=self.revision_id,
            transaction_at=self.transaksjons_tidspunkt,
            store_name=self.store_name,
            chain=self.chain,
            total_amount=self.belop,
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


def _scan_bracketed_json_slice(html: str, start: int, *, escaped: bool) -> int | None:
    """Return end index of the balanced JSON container starting at `start`, or None."""
    open_c = html[start]
    close_c = "]" if open_c == "[" else "}"
    depth = 0
    in_str = False
    json_escaped = False
    j = start
    n = len(html)

    while j < n:
        c = html[j]
        if escaped:
            if c == "\\" and j + 1 < n:
                nxt = html[j + 1]
                if nxt == '"':
                    if not in_str:
                        in_str = True
                        json_escaped = False
                    elif json_escaped:
                        json_escaped = False
                    else:
                        in_str = False
                        json_escaped = False
                elif in_str and nxt == "\\":
                    json_escaped = not json_escaped
                else:
                    json_escaped = False
                j += 2
                continue
            json_escaped = False
            if not in_str:
                if c == open_c:
                    depth += 1
                elif c == close_c:
                    depth -= 1
                    if depth == 0:
                        return j + 1
            j += 1
        else:
            if in_str:
                if c == "\\":
                    j += 2
                    continue
                if c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == open_c:
                    depth += 1
                elif c == close_c:
                    depth -= 1
                    if depth == 0:
                        return j + 1
            j += 1

    return None


def _decode_rsc_raw_slice(raw: str, *, escaped: bool) -> str:
    if not escaped:
        return re.sub(r'"\$D', '"', raw)
    try:
        decoded = json.loads(f'"{raw}"', strict=False)
    except json.JSONDecodeError:
        decoded = re.sub(
            r"\\(.)",
            lambda m: m.group(1) if m.group(1) in ('"', "\\", "/") else m.group(0),
            raw,
        )
    return re.sub(r'"\$D', '"', decoded)


def _extract_rsc_flight_entry(html: str, key: str) -> tuple[bool, Any]:
    """Extract (found_key, parsed_value) for `key` in an RSC flight payload."""
    escaped_marker = f'\\"{key}\\":'
    unescaped_marker = f'"{key}":'

    saw_null_or_undefined = False
    for marker, is_escaped in ((escaped_marker, True), (unescaped_marker, False)):
        search_from = 0
        while True:
            at = html.find(marker, search_from)
            if at < 0:
                break
            i = at + len(marker)
            while i < len(html) and html[i].isspace():
                i += 1
            if i >= len(html):
                break

            if html.startswith("null", i):
                saw_null_or_undefined = True
                search_from = i + 4
                continue

            undef_lit = '\\"$undefined\\"' if is_escaped else '"$undefined"'
            if html.startswith(undef_lit, i):
                saw_null_or_undefined = True
                search_from = i + len(undef_lit)
                continue

            if html[i] in "[{":
                end_idx = _scan_bracketed_json_slice(html, i, escaped=is_escaped)
                if end_idx is None:
                    search_from = i + 1
                    continue
                decoded = _decode_rsc_raw_slice(html[i:end_idx], escaped=is_escaped)
                try:
                    return True, json.loads(decoded)
                except json.JSONDecodeError as err:
                    raise UnsupportedTrumfContract(f"Malformed JSON in RSC payload: {err}") from err

            # Non-container literal (e.g., string/number/boolean)
            end_m = re.search(r"[,}\]]", html[i:])
            lit_end = i + end_m.start() if end_m else len(html)
            raw_lit = html[i:lit_end].strip()
            decoded_lit = _decode_rsc_raw_slice(raw_lit, escaped=is_escaped)
            try:
                return True, json.loads(decoded_lit)
            except json.JSONDecodeError as err:
                raise UnsupportedTrumfContract(f"Malformed JSON in RSC payload: {err}") from err

    if saw_null_or_undefined:
        return True, None
    return False, None


def extract_rsc_flight_json(html: str, key: str) -> Any | None:
    """Extract and parse a JSON array or object following key in an RSC flight payload.

    Next.js App Router RSC streams flight data inside script tags with escaped quotes (\\").
    This parser searches for the key marker, performs string-aware bracket matching,
    unescapes quotes and backslashes, strips the RSC '$D' date prefix, and parses JSON.
    Returns None if the key marker or structure is not found.
    """
    _, value = _extract_rsc_flight_entry(html, key)
    return value


def _is_unauthenticated_dict(data: dict[str, Any]) -> bool:
    if data.get("authenticated") is False:
        return True
    status = data.get("status") or data.get("statusCode")
    if status in (401, 403):
        return True
    error = str(
        data.get("error") or data.get("message") or data.get("title") or data.get("detail") or ""
    ).casefold()
    return any(
        marker in error
        for marker in (
            "unauthorized",
            "unauthenticated",
            "forbidden",
            "logg inn",
            "innlogging",
            "login required",
        )
    )


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
        if _is_unauthenticated_dict(payload):
            raise TrumfAuthenticationError(
                "Missing or invalid Trumf session payload; unauthenticated response from provider"
            )
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
        if stripped.startswith(("{", "[")) and "__next_f" not in payload:
            try:
                parsed_json = json.loads(stripped)
            except json.JSONDecodeError as err:
                raise UnsupportedTrumfContract(
                    f"Malformed JSON in Trumf transaction payload: {err}"
                ) from err
            return parse_transactions(parsed_json)
        found, extracted = _extract_rsc_flight_entry(payload, "transactionsData")
        if not found:
            raise TrumfAuthenticationError(
                "Missing or invalid Trumf session payload; no transaction data found in page"
            )
        raw_list = extracted
    else:
        raise UnsupportedTrumfContract(f"Unsupported payload type: {type(payload).__name__}")

    if not isinstance(raw_list, list):
        raise UnsupportedTrumfContract("Trumf transactions data must be an array")

    try:
        return [TrumfTransaction.model_validate(item) for item in raw_list]
    except ValidationError as err:
        raise UnsupportedTrumfContract(f"Invalid transaction in Trumf list: {err}") from err


def _has_non_missing_key(mapping: dict[str, Any], *keys: str) -> bool:
    return any(key in mapping and not _is_missing_rsc_value(mapping[key]) for key in keys)


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
        if _is_unauthenticated_dict(payload):
            raise TrumfAuthenticationError(
                "Missing or invalid Trumf session payload; unauthenticated response from provider"
            )
        if "purchaseDetails" in payload:
            raw_dict = payload["purchaseDetails"]
        elif "summary" in payload:
            raw_dict = payload["summary"]
        else:
            raw_dict = payload
    elif isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("{") and "__next_f" not in payload:
            try:
                parsed_json = json.loads(stripped)
            except json.JSONDecodeError as err:
                raise UnsupportedTrumfContract(
                    f"Malformed JSON in Trumf receipt payload: {err}"
                ) from err
            return parse_receipt(parsed_json, transaction=transaction)
        found, extracted = _extract_rsc_flight_entry(payload, "purchaseDetails")
        if not found:
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
        if (
            not _has_non_missing_key(enriched, "batchId", "batchid", "batch_id")
            and transaction.batch_id
        ):
            enriched["batchId"] = transaction.batch_id
        if (
            not _has_non_missing_key(enriched, "storeName", "store_name", "beskrivelse")
            and transaction.beskrivelse
        ):
            enriched["storeName"] = transaction.beskrivelse
        if not _has_non_missing_key(enriched, "chain", "filterCategory", "kjedeid"):
            chain = chain_of(transaction)
            if chain:
                enriched["chain"] = chain
        if not _has_non_missing_key(
            enriched,
            "transaksjonsTidspunkt",
            "dato",
            "transaksjons_tidspunkt",
            "purchaseDate",
        ):
            enriched["transaksjonsTidspunkt"] = transaction.transaksjons_tidspunkt
        if not _has_non_missing_key(enriched, "belop", "amount", "total"):
            enriched["belop"] = transaction.belop
        if not _has_non_missing_key(enriched, "bonus") and transaction.bonus is not None:
            enriched["bonus"] = transaction.bonus
        if not _has_non_missing_key(enriched, "hasReceipt", "has_receipt", "harKvittering"):
            enriched["hasReceipt"] = transaction.har_kvittering

    if "revision_id" not in enriched:
        enriched["revision_id"] = _revision_id(enriched)

    try:
        return TrumfReceipt.model_validate(enriched)
    except ValidationError as err:
        raise UnsupportedTrumfContract(f"Invalid Trumf receipt structure: {err}") from err


def build_transaction_receipt_stub(transaction: TrumfTransaction) -> TrumfReceipt:
    """Build a provider record stub for a transaction that lacks receipt detail.

    This preserves provider transaction evidence while explicitly marking has_receipt=False
    and empty varelinjer, preventing promotion to confirmed kitchen purchases.
    """
    effective_batch_id = (
        transaction.batch_id.strip()
        if isinstance(transaction.batch_id, str) and transaction.batch_id.strip()
        else "tx:"
        + hashlib.sha256(
            f"{transaction.transaksjons_tidspunkt.isoformat()}|{transaction.beskrivelse}|{transaction.belop}".encode()
        ).hexdigest()[:24]
    )
    raw: dict[str, Any] = {
        "batchId": effective_batch_id,
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
    external_id = receipt.standardize().external_id
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


def _validate_trumf_batch_id(batch_id: str) -> str:
    if not isinstance(batch_id, str):
        raise UnsupportedTrumfContract("Ugyldig Trumf batchId")
    cleaned = batch_id.strip()
    if (
        not cleaned
        or cleaned == "$undefined"
        or ".." in cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or any(ord(ch) < 32 for ch in cleaned)
    ):
        raise UnsupportedTrumfContract(f"Ugyldig Trumf batchId: {batch_id!r}")
    return cleaned


class TrumfHttpSource:
    """Live HTTP source for Trumf kvitteringer using session cookies."""

    def __init__(
        self,
        session_cookie: str,
        *,
        base_url: str = "https://www.trumf.no",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        raw_cookie = session_cookie if isinstance(session_cookie, str) else ""
        clean_cookie = _normalize_cookie_header(raw_cookie)
        if not clean_cookie:
            raise TrumfAuthenticationError("Trumf-informasjonskapsel mangler.")
        self.session_cookie = clean_cookie
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self._tx_cache: dict[str, TrumfTransaction] = {}

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=self.transport,
            timeout=25.0,
            follow_redirects=False,
            headers={
                "Cookie": self.session_cookie,
                "Accept": "text/html, text/x-component, application/json",
                "User-Agent": "HomeKitchen/1.0",
            },
        )

    def get_transactions(self, account_id: str) -> list[TrumfTransaction]:
        url = f"{self.base_url}/trumf-profil/kvitteringer"
        with self._client() as client:
            resp = client.get(url)
        if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
            raise TrumfAuthenticationError(
                "Trumf-sesjonen er utløpt eller ugyldig; forny eller koble til på nytt i Innstillinger."
            )
        if resp.status_code >= 400:
            raise UnsupportedTrumfContract(f"Trumf svarte med HTTP {resp.status_code}")
        txs = parse_transactions(resp.text)
        for tx in txs:
            if tx.batch_id:
                self._tx_cache[tx.batch_id] = tx
        return txs

    def get_receipt(self, account_id: str, batch_id: str) -> TrumfReceipt | None:
        clean_batch_id = _validate_trumf_batch_id(batch_id)
        encoded_batch_id = urllib.parse.quote(clean_batch_id, safe="")
        url = f"{self.base_url}/trumf-profil/kvitteringer/{encoded_batch_id}"
        with self._client() as client:
            resp = client.get(url)
        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
            raise TrumfAuthenticationError(
                "Trumf-sesjonen er utløpt eller ugyldig; forny eller koble til på nytt i Innstillinger."
            )
        if resp.status_code >= 400:
            raise UnsupportedTrumfContract(
                f"Trumf kvittering {clean_batch_id} svarte med HTTP {resp.status_code}"
            )
        if "__next_f" in resp.text:
            found, extracted = _extract_rsc_flight_entry(resp.text, "purchaseDetails")
            if found and extracted is None:
                return None
        return parse_receipt(resp.text, transaction=self._tx_cache.get(clean_batch_id))


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
        self._scan_snapshots: dict[str, list[TrumfTransaction]] = {}

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
            self._scan_snapshots = {scan.scan_id: transactions}
        else:
            transactions = self._scan_snapshots.get(scan.scan_id)
            if transactions is None:
                transactions = self.source.get_transactions(account_id)
                self._scan_snapshots[scan.scan_id] = transactions

        page_num = scan.next_page
        start_idx = (page_num - 1) * self.page_size
        end_idx = start_idx + self.page_size
        page_transactions = transactions[start_idx:end_idx]

        has_more_records = end_idx < len(transactions)
        truncated = page_num == self.max_pages and has_more_records
        has_more = has_more_records and not truncated

        records: list[ProviderOrderRecord] = []
        for tx in page_transactions:
            if tx.har_kvittering and tx.batch_id:
                receipt = self.source.get_receipt(account_id, tx.batch_id)
                if receipt is not None:
                    if not receipt.batch_id:
                        receipt = receipt.model_copy(update={"batch_id": tx.batch_id})
                    records.append(to_provider_order_record(receipt))
                elif self.include_non_receipt_transactions:
                    stub = build_transaction_receipt_stub(tx)
                    records.append(to_provider_order_record(stub))
            elif self.include_non_receipt_transactions:
                stub = build_transaction_receipt_stub(tx)
                records.append(to_provider_order_record(stub))

        if not has_more:
            self._scan_snapshots.pop(scan.scan_id, None)

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
