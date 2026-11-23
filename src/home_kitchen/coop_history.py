"""Read-only normalization for documented Coop purchase history payloads.

The mapping is based on the reverse-engineered Coop Android 4.17.3 schema.
It does not authenticate, fetch provider data, or assert purchase completion.
"""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, urlsafe_b64encode
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol, runtime_checkable
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from home_kitchen.purchase_sync import ProviderOrderRecord, ProviderPage, extract_line_ean
from home_kitchen.receipts import Receipt, ReceiptLine

COOP_SCHEMA_SOURCE = "https://gist.github.com/HelgeSverre/80a7f34f874336324184a0c513c2e6a2"
COOP_SCHEMA_VERSION = "4.17.3"
DEFAULT_COOP_BASE_URL = "https://coopay.coop.no"
DEFAULT_COOP_USER_AGENT = "Android(14)/Coop(4.42.0)/2.7.7"
DEFAULT_COOP_LOCALE = "nb_NO"
DEFAULT_COOP_PURCHASES_COUNT = 50
DEFAULT_COOP_SEARCH_MAX_PAGES = 100
COOP_NORMALIZER_VERSION = "3"
_COOP_LOCAL_TIMEZONE = ZoneInfo("Europe/Oslo")
_COOP_NORWEGIAN_MONTHS = {
    "januar": 1,
    "jan": 1,
    "februar": 2,
    "feb": 2,
    "mars": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "mai": 5,
    "juni": 6,
    "jun": 6,
    "juli": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "oktober": 10,
    "okt": 10,
    "november": 11,
    "nov": 11,
    "desember": 12,
    "des": 12,
}
_COOP_AUTH_ERROR_CODES = frozenset(
    {
        "INVALID_TOKEN",
        "NO_REFRESH_TOKEN",
        "REFRESH_ACCESS_TOKEN",
        "UNAUTHORIZED",
        "EXPIRED_TOKEN",
    }
)


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


class UnsupportedCoopContract(ValueError):
    """The observed response needs a provider contract not present in the spec."""


class CoopAuthenticationError(RuntimeError):
    """Missing or invalid Coop credentials or unauthenticated response."""


def _parse_coop_purchase_datetime(
    value: Any, *, history_year: int | None = None, history_month: int | None = None
) -> datetime | None:
    """Parse full timestamps, or add a year only from the provider's month grouping.

    Coop's history search groups stubs by an authoritative month and year. Some
    fields omit the year, so they are usable only when that grouping is present.
    A genuinely absent date stays absent; the current date is never consulted.
    """

    def apply_history_context(parsed: datetime) -> datetime:
        if history_month is not None and parsed.month != history_month:
            raise UnsupportedCoopContract(
                "Coop purchase timestamp month conflicts with its history grouping"
            )
        if history_year is not None and parsed.year != history_year:
            try:
                parsed = parsed.replace(year=history_year)
            except ValueError:
                raise UnsupportedCoopContract(
                    "Coop purchase timestamp conflicts with its history year"
                ) from None
        return parsed

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value.replace(tzinfo=_COOP_LOCAL_TIMEZONE) if value.tzinfo is None else value
        return apply_history_context(parsed)
    if not isinstance(value, str):
        raise UnsupportedCoopContract("Coop purchase timestamp has an unsupported value")

    normalized = value.strip()
    if not normalized:
        return None

    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        parsed = parsed.replace(tzinfo=_COOP_LOCAL_TIMEZONE) if parsed.tzinfo is None else parsed
        return apply_history_context(parsed)

    # Full Norwegian timestamp (optionally seconds), with an explicit year.
    for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(normalized, pattern)
        except ValueError:
            continue
        return apply_history_context(parsed.replace(tzinfo=_COOP_LOCAL_TIMEZONE))

    # Coop's nb_NO history search can return dates such as "10. november 18:59".
    # A missing year is supplied only by the authoritative search grouping.
    named_month_match = re.fullmatch(
        r"(?P<day>\d{1,2})\.\s+(?P<month_name>[A-Za-zÆØÅæøå]+)\.?"
        r"(?:\s+(?P<year>\d{4}))?"
        r"(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?",
        normalized,
        re.IGNORECASE,
    )
    if named_month_match:
        month = _COOP_NORWEGIAN_MONTHS.get(named_month_match.group("month_name").casefold())
        if month is not None:
            year_value = named_month_match.group("year")
            year = int(year_value) if year_value is not None else history_year
            if year is None:
                return None
            if history_month is not None and month != history_month:
                raise UnsupportedCoopContract(
                    "Coop purchase timestamp month conflicts with its history grouping"
                )
            try:
                parsed = datetime(
                    year,
                    month,
                    int(named_month_match.group("day")),
                    int(named_month_match.group("hour") or 0),
                    int(named_month_match.group("minute") or 0),
                    int(named_month_match.group("second") or 0),
                    tzinfo=_COOP_LOCAL_TIMEZONE,
                )
            except ValueError:
                raise UnsupportedCoopContract(
                    "Coop purchase timestamp has an invalid date"
                ) from None
            return apply_history_context(parsed)

    # Norwegian timestamps that omit the year are resolvable only with the
    # authoritative month/year supplied by the search grouping. Parse numeric
    # parts directly so Python never supplies an implicit current year.
    partial_match = re.fullmatch(
        r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?",
        normalized,
    )
    if partial_match:
        day = int(partial_match.group("day"))
        month = int(partial_match.group("month"))
        hour = int(partial_match.group("hour") or 0)
        minute = int(partial_match.group("minute") or 0)
        second = int(partial_match.group("second") or 0)
        if history_year is None:
            return None
        if history_month is not None and month != history_month:
            raise UnsupportedCoopContract(
                "Coop purchase timestamp month conflicts with its history grouping"
            )
        try:
            return datetime(
                history_year,
                month,
                day,
                hour,
                minute,
                second,
                tzinfo=_COOP_LOCAL_TIMEZONE,
            )
        except ValueError:
            raise UnsupportedCoopContract("Coop purchase timestamp has an invalid date") from None

    # Time-only values cannot identify a calendar date. Treat them as missing
    # rather than assigning today's date or inventing a day from the grouping.
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", normalized):
        return None
    raise UnsupportedCoopContract("Coop purchase timestamp has an unsupported format")


def _purchase_date_payload(
    raw: dict[str, Any], *, history_year: int | None = None, history_month: int | None = None
) -> dict[str, Any]:
    """Copy a provider summary and normalize whichever timestamp field it uses."""
    normalized = dict(raw)
    timestamp_keys = ("purchaseDate", "purchase_date", "dateTime", "date_time", "date")
    timestamp_key = next(
        (key for key in timestamp_keys if key in raw and raw[key] is not None and raw[key] != ""),
        None,
    )
    if timestamp_key is not None:
        parsed = _parse_coop_purchase_datetime(
            raw[timestamp_key], history_year=history_year, history_month=history_month
        )
        normalized["purchaseDate"] = parsed
    elif any(key in raw for key in timestamp_keys):
        normalized["purchaseDate"] = None
    return normalized


def _normalize_coop_decimal(value: Any) -> Any:
    """Normalize Coop decimal strings, including spaced thousands separators."""
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        # Grouping must be valid: never turn malformed "12 34" into "1234".
        if re.fullmatch(r"[+-]?\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?", normalized):
            normalized = re.sub(r"[ \u00a0\u202f]", "", normalized)
        return normalized.replace(",", ".")
    return value


# The validator must wrap the nullable union, so "" can normalize to None.
type CoopDecimal = Annotated[Decimal | None, BeforeValidator(_normalize_coop_decimal)]


def _split_coop_quantity(value: Any) -> tuple[Any, str | None]:
    if isinstance(value, str):
        parts = value.strip().rsplit(maxsplit=1)
        if len(parts) == 2:
            number, unit = parts
            if any(character.isalpha() for character in unit):
                return number, unit
    return value, None


class CoopQuantity(BaseModel):
    """Coop receipt quantity with its optional source unit."""

    model_config = ConfigDict(extra="ignore")

    value: CoopDecimal = None
    unit: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _parse_source_quantity(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return value
        number, unit = _split_coop_quantity(value)
        return {"value": _normalize_coop_decimal(number), "unit": unit}


def _normalize_coop_quantity_input(value: Any) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


type CoopQuantityInput = Annotated[
    CoopQuantity | None, BeforeValidator(_normalize_coop_quantity_input)
]


class CoopReceiptLine(BaseModel):
    model_config = ConfigDict(extra="ignore", alias_generator=_to_camel, populate_by_name=True)

    product_name: str | None = Field(
        default=None, validation_alias=AliasChoices("productName", "name")
    )
    ean13: str | None = None
    gtin13: str | None = None
    barcode: str | None = None
    quantity: CoopQuantityInput = None
    amount: CoopDecimal = Field(default=None, validation_alias=AliasChoices("amount", "price"))
    discount: CoopDecimal = None
    discount_amount_per_quantity: CoopDecimal = None
    unit_price: CoopDecimal = None

    @model_validator(mode="before")
    @classmethod
    def _attach_explicit_quantity_unit(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        explicit_unit = value.get("quantityUnit") or value.get("quantity_unit")
        raw_quantity = value.get("quantity")
        if not explicit_unit or raw_quantity is None:
            return value
        normalized = dict(value)
        if isinstance(raw_quantity, dict):
            if raw_quantity.get("unit"):
                return value
            quantity_value = raw_quantity.get("value")
            number, embedded_unit = _split_coop_quantity(quantity_value)
            normalized["quantity"] = {
                "value": number,
                "unit": embedded_unit or explicit_unit,
            }
            return normalized
        number, embedded_unit = _split_coop_quantity(raw_quantity)
        if embedded_unit:
            return value
        normalized["quantity"] = {"value": number, "unit": explicit_unit}
        return normalized

    def standardize(self) -> ReceiptLine:
        quantity = self.quantity
        return ReceiptLine(
            name=self.product_name or "Unknown item",
            raw_name=self.product_name,
            quantity=quantity.value if quantity is not None else None,
            unit=quantity.unit if quantity is not None else None,
            amount=self.amount,
            ean=extract_line_ean(
                {"ean13": self.ean13, "gtin13": self.gtin13, "barcode": self.barcode}
            ),
            is_return=(quantity is not None and quantity.value is not None and quantity.value < 0)
            or (self.amount is not None and self.amount < 0),
        )


class CoopReceipt(BaseModel):
    """Typed detail receipt retaining provider IDs and nullable source fields."""

    model_config = ConfigDict(extra="ignore", alias_generator=_to_camel, populate_by_name=True)

    summary_id: str | None = None
    receipt_id: str | None = None
    purchase_date: AwareDatetime | None = None
    store_name: str | None = None
    store_id: str | None = None
    chain_id: str | None = None
    amount: CoopDecimal = Field(default=None, validation_alias=AliasChoices("amount", "totalSum"))
    total_discount: CoopDecimal = None
    total_discount_formatted: str | None = None
    member_bonus: CoopDecimal = None
    lines: list[CoopReceiptLine] | None = None
    source_uri: str = COOP_SCHEMA_SOURCE
    source_schema_version: str = COOP_SCHEMA_VERSION
    source_updated_at: None = None
    revision_id: str

    def standardize(self) -> Receipt:
        lines = [line.standardize() for line in self.lines or []]
        return Receipt(
            provider="coop",
            external_id=self.summary_id or self.receipt_id,
            revision_id=self.revision_id,
            transaction_at=self.purchase_date,
            store_name=self.store_name,
            store_id=self.store_id,
            chain=self.chain_id,
            total_amount=self.amount,
            total_discount=self.total_discount,
            has_receipt=bool(lines),
            lines=lines,
            source_uri=self.source_uri,
            source_schema_version=self.source_schema_version,
            source_updated_at=self.source_updated_at,
        )


class CoopPurchaseStub(BaseModel):
    model_config = ConfigDict(extra="ignore", alias_generator=_to_camel, populate_by_name=True)

    summary_id: str | None = None
    purchase_date: AwareDatetime | None = None
    history_year: int | None = Field(default=None, exclude=True)
    history_month: int | None = Field(default=None, exclude=True)
    store_name: str | None = None
    store_id: str | None = None
    chain_id: str | None = None
    amount: CoopDecimal = Field(default=None, validation_alias=AliasChoices("amount", "totalSum"))
    total_discount: CoopDecimal = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_timestamp(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        year = value.get("historyYear", value.get("history_year"))
        month = value.get("historyMonth", value.get("history_month"))
        return _purchase_date_payload(value, history_year=year, history_month=month)


def _revision_id(
    raw: dict[str, Any], *, history_year: int | None = None, history_month: int | None = None
) -> str:
    # Bump the normalizer identity so a corrected interpretation of an
    # unchanged historical payload is stored as a new immutable revision.
    revision_input = {
        "normalizer_version": COOP_NORMALIZER_VERSION,
        "history_year": history_year,
        "history_month": history_month,
        "source": raw,
    }
    encoded = json.dumps(revision_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_purchase_list(
    response: dict[str, Any], *, continuation_observed: bool = False
) -> list[CoopPurchaseStub]:
    """Parse a legacy `{purchases: [...]}` envelope used by offline fixtures.

    Live history ingestion uses :func:`parse_coop_search_page` instead.
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
    response: dict[str, Any],
    *,
    continuation_observed: bool = False,
    history_year: int | None = None,
    history_month: int | None = None,
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
    normalized = _purchase_date_payload(raw, history_year=history_year, history_month=history_month)
    return CoopReceipt.model_validate(
        {
            **normalized,
            "revision_id": _revision_id(
                raw, history_year=history_year, history_month=history_month
            ),
        }
    )


def _history_month_context(value: Any) -> tuple[int, int]:
    if not isinstance(value, dict):
        raise UnsupportedCoopContract("Coop history month grouping must be an object")
    year = value.get("year")
    month = value.get("month")
    if (
        isinstance(year, bool)
        or isinstance(month, bool)
        or not isinstance(year, (int, str))
        or not isinstance(month, (int, str))
    ):
        raise UnsupportedCoopContract("Coop history month grouping has invalid year/month")
    try:
        year, month = int(year), int(month)
    except (TypeError, ValueError):
        raise UnsupportedCoopContract(
            "Coop history month grouping has invalid year/month"
        ) from None
    if not 1900 <= year <= 2200 or not 1 <= month <= 12:
        raise UnsupportedCoopContract("Coop history month grouping has invalid year/month")
    return year, month


def _coop_search_shape_hint(response: Any) -> str:
    """Describe an unexpected response without logging receipt contents."""
    if not isinstance(response, dict):
        return f"received {type(response).__name__}"
    parts = []
    for key, value in sorted(response.items(), key=lambda item: str(item[0]))[:16]:
        safe_key = re.sub(r"[^A-Za-z0-9_.-]", "?", str(key))[:48]
        parts.append(f"{safe_key}:{type(value).__name__}")
    if len(response) > 16:
        parts.append("...")
    return "top-level " + ", ".join(parts)


def parse_coop_search_page(response: Any) -> tuple[list[CoopPurchaseStub], str | None]:
    """Parse the observed month-grouped search envelope and its continuation token."""
    if not isinstance(response, dict):
        raise UnsupportedCoopContract(
            f"unsupported Coop history search response shape ({_coop_search_shape_hint(response)})"
        )
    allowed = {
        "hitsPerMonth",
        "continuationToken",
        "searchText",
        "resultCode",
        "errorCode",
        "errorMessage",
    }
    search_text = response.get("searchText")
    if (
        set(response) - allowed
        or (search_text is not None and not isinstance(search_text, str))
        or not isinstance(response.get("hitsPerMonth"), list)
    ):
        raise UnsupportedCoopContract(
            f"unsupported Coop history search response shape ({_coop_search_shape_hint(response)})"
        )
    if "continuationToken" not in response:
        raise UnsupportedCoopContract("Coop history search response is missing continuationToken")

    token = response["continuationToken"]
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise UnsupportedCoopContract("Coop history search continuation token is malformed")

    purchases: list[CoopPurchaseStub] = []
    for group in response["hitsPerMonth"]:
        if not isinstance(group, dict) or set(group) - {"monthOfYear", "periodPurchases"}:
            raise UnsupportedCoopContract("unsupported Coop history month grouping shape")
        year, month = _history_month_context(group.get("monthOfYear"))
        period_purchases = group.get("periodPurchases")
        if not isinstance(period_purchases, list):
            raise UnsupportedCoopContract("Coop periodPurchases must be an array")
        for purchase in period_purchases:
            if not isinstance(purchase, dict):
                raise UnsupportedCoopContract("Coop history purchase summary must be an object")
            summary_id = purchase.get("summaryId", purchase.get("summary_id"))
            if not isinstance(summary_id, str) or not summary_id.strip():
                raise UnsupportedCoopContract(
                    "Coop history purchase summary is missing summaryId; scan is incomplete"
                )
            enriched = _purchase_date_payload(purchase, history_year=year, history_month=month)
            enriched["historyYear"] = year
            enriched["historyMonth"] = month
            purchases.append(CoopPurchaseStub.model_validate(enriched))
    return purchases, token.strip() if isinstance(token, str) and token.strip() else None


def to_provider_order_record(receipt: CoopReceipt) -> ProviderOrderRecord:
    """Convert one normalized detail snapshot into the neutral immutable record.

    Coop's documented schema has no source update timestamp. The ingestion hash
    distinguishes changed detail snapshots, while observed_at is recorded by the
    neutral persistence layer.
    """
    standardized = receipt.standardize()
    external_id = standardized.external_id
    if not external_id:
        raise UnsupportedCoopContract("Coop receipt has no summaryId or receiptId")
    return ProviderOrderRecord(
        external_id=external_id,
        revision_id=receipt.revision_id,
        source_updated_at=None,
        payload=receipt.model_dump(mode="json", by_alias=True),
    )


COOP_AUTH_METHOD = "oidc_pkce"
COOP_LIMITATIONS = (
    "Coop purchase history is derived from Android APK 4.17.3 OpenAPI reverse engineering. "
    "Auth uses Auth0 OIDC with PKCE S256 and offline_access. "
    "History search continuation and month-grouped summaries are based on observed wire responses; "
    "no source update timestamp is reported."
)


class CoopAdapterStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["coop"] = "coop"
    auth_method: Literal["oidc_pkce"] = "oidc_pkce"
    read_only: bool = True
    live_calls_enabled: bool = False
    limitations: str = COOP_LIMITATIONS


class _CoopScanCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    mode: Literal["full_rescan"]
    account_id: str
    scan_id: str
    next_page: int = Field(ge=1, le=50)
    state: Literal["scanning", "complete", "incomplete"]


def _encode_coop_cursor(cursor: _CoopScanCursor) -> str:
    return "coop-scan-v1." + urlsafe_b64encode(cursor.model_dump_json().encode()).decode()


def _decode_coop_cursor(value: str) -> _CoopScanCursor:
    try:
        prefix, encoded = value.split(".", 1)
        if prefix != "coop-scan-v1" or len(value) > 2048:
            raise ValueError
        cursor = _CoopScanCursor.model_validate_json(
            b64decode(encoded, altchars=b"-_", validate=True)
        )
        UUID(cursor.scan_id)
        if cursor.state != "scanning" and cursor.next_page != 1:
            raise ValueError
        return cursor
    except (ValueError, TypeError):
        raise ValueError("Invalid Coop history scan cursor") from None


@runtime_checkable
class CoopSource(Protocol):
    """Protocol for reading Coop purchases and receipts without network coupling."""

    def get_purchases(self, account_id: str) -> list[CoopPurchaseStub]: ...

    def get_receipt(
        self,
        account_id: str,
        summary_id: str,
        *,
        history_year: int | None = None,
        history_month: int | None = None,
    ) -> CoopReceipt | None: ...


class CoopFixtureSource:
    """Fixture-backed offline source for Coop purchases and receipts."""

    def __init__(
        self,
        purchases: list[dict[str, Any]] | list[CoopPurchaseStub],
        receipts: dict[str, dict[str, Any] | CoopReceipt] | None = None,
        *,
        failure: Exception | None = None,
    ) -> None:
        self._failure = failure
        self._purchases: list[CoopPurchaseStub] = [
            p if isinstance(p, CoopPurchaseStub) else CoopPurchaseStub.model_validate(p)
            for p in purchases
        ]
        self._receipts: dict[str, dict[str, Any] | CoopReceipt] = receipts or {}
        self.purchase_calls = 0
        self.receipt_calls: list[str] = []

    def get_purchases(self, account_id: str) -> list[CoopPurchaseStub]:
        self.purchase_calls += 1
        if self._failure is not None:
            raise self._failure
        return list(self._purchases)

    def get_receipt(
        self,
        account_id: str,
        summary_id: str,
        *,
        history_year: int | None = None,
        history_month: int | None = None,
    ) -> CoopReceipt | None:
        self.receipt_calls.append(summary_id)
        if self._failure is not None:
            raise self._failure
        raw = self._receipts.get(summary_id)
        if raw is None:
            return None
        if isinstance(raw, CoopReceipt):
            return raw
        return parse_receipt_detail(raw, history_year=history_year, history_month=history_month)


def _check_coop_base_response(data: Any) -> None:
    """Validate Coop TBaseResponse resultCode/errorCode body errors even on HTTP 200."""
    if not isinstance(data, dict):
        return
    result_code = data.get("resultCode")
    if not isinstance(result_code, str):
        return
    normalized_result = result_code.strip().upper()
    if not normalized_result or normalized_result in ("OK", "SUCCESS"):
        return
    error_code = str(data.get("errorCode") or "").strip()
    upper_code = error_code.upper()
    if upper_code in _COOP_AUTH_ERROR_CODES or "TOKEN" in upper_code or "AUTH" in upper_code:
        raise CoopAuthenticationError(
            "Coop-sesjonen er utløpt eller ugyldig; forny eller koble til på nytt i Innstillinger."
        )
    detail = error_code or str(data.get("errorMessage") or "ukjent feil").strip()
    raise UnsupportedCoopContract(f"Coop svarte med {normalized_result}: {detail}")


class CoopHttpSource:
    """Live HTTP source for Coop purchases and digital receipts (coopay.coop.no)."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        session_cookie: str | None = None,
        base_url: str = DEFAULT_COOP_BASE_URL,
        user_agent: str = DEFAULT_COOP_USER_AGENT,
        locale: str = DEFAULT_COOP_LOCALE,
        device_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
        max_search_pages: int = DEFAULT_COOP_SEARCH_MAX_PAGES,
    ) -> None:
        clean_token = access_token.strip() if isinstance(access_token, str) else ""
        clean_cookie = session_cookie.strip() if isinstance(session_cookie, str) else ""
        if not clean_token and not clean_cookie:
            raise CoopAuthenticationError("Coop-tilgangstoken eller informasjonskapsel mangler.")
        clean_device_id = device_id.strip() if isinstance(device_id, str) else ""
        if not clean_device_id:
            seed = clean_token or clean_cookie
            clean_device_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        if max_search_pages < 1 or max_search_pages > 1000:
            raise ValueError("max_search_pages must be between 1 and 1000")
        self.access_token = clean_token or None
        self.session_cookie = clean_cookie or None
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.locale = locale
        self.device_id = clean_device_id
        self.transport = transport
        self.max_search_pages = max_search_pages

    def _client(self) -> httpx.Client:
        headers = {
            "Accept": "application/json;charset=UTF-8",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": self.user_agent,
            "X-DeviceId": self.device_id,
            "X-GLTLocale": self.locale,
        }
        if self.access_token:
            headers["X-Token"] = self.access_token
        elif self.session_cookie:
            headers["Cookie"] = self.session_cookie
        return httpx.Client(
            transport=self.transport,
            timeout=25.0,
            follow_redirects=False,
            headers=headers,
        )

    def _raise_for_response_status(
        self,
        resp: httpx.Response,
        *,
        allow_404: bool = False,
        context: str = "Coop",
    ) -> None:
        if (
            resp.status_code == 403
            and resp.headers.get("x-client-upgrade", "").strip().lower() == "force"
        ):
            raise UnsupportedCoopContract(
                f"{context} krevde app-oppgradering (HTTP 403 x-client-upgrade: force)"
            )
        if resp.status_code in (401, 403):
            raise CoopAuthenticationError(
                "Coop-sesjonen er utløpt eller ugyldig; forny eller koble til på nytt i Innstillinger."
            )
        if resp.status_code == 404 and allow_404:
            return
        if resp.status_code >= 400:
            raise UnsupportedCoopContract(f"{context} svarte med HTTP {resp.status_code}")

    def get_purchases(self, account_id: str) -> list[CoopPurchaseStub]:
        with self._client() as client:
            response = client.get(f"{self.base_url}/user/pay/history/search", params={"text": ""})
            purchases: list[CoopPurchaseStub] = []
            summary_contexts: dict[str, tuple[int | None, int | None]] = {}
            seen_tokens: set[str] = set()

            for page_number in range(1, self.max_search_pages + 1):
                self._raise_for_response_status(response, context="Coop history search")
                payload = response.json()
                _check_coop_base_response(payload)
                page_purchases, next_token = parse_coop_search_page(payload)
                for purchase in page_purchases:
                    summary_id = purchase.summary_id
                    if summary_id:
                        context = (purchase.history_year, purchase.history_month)
                        previous_context = summary_contexts.get(summary_id)
                        if previous_context is not None:
                            if previous_context != context:
                                raise UnsupportedCoopContract(
                                    "Coop repeated a summaryId with conflicting history month context; "
                                    "scan is incomplete"
                                )
                            continue
                        summary_contexts[summary_id] = context
                    purchases.append(purchase)

                if next_token is None:
                    return purchases
                if next_token in seen_tokens:
                    raise UnsupportedCoopContract(
                        "Coop history search repeated a continuation token; scan is incomplete"
                    )
                if page_number == self.max_search_pages:
                    raise UnsupportedCoopContract(
                        "Coop history search exceeded its page limit; scan is incomplete"
                    )
                seen_tokens.add(next_token)
                response = client.get(
                    f"{self.base_url}/user/pay/history/search/continue",
                    params={"token": next_token},
                )

        raise AssertionError("unreachable Coop history search state")

    def get_receipt(
        self,
        account_id: str,
        summary_id: str,
        *,
        history_year: int | None = None,
        history_month: int | None = None,
    ) -> CoopReceipt | None:
        url = f"{self.base_url}/user/pay/history/details"
        with self._client() as client:
            resp = client.get(url, params={"summaryId": summary_id})
        if resp.status_code == 404:
            return None
        self._raise_for_response_status(resp, context=f"Coop kvittering {summary_id}")
        payload = resp.json()
        _check_coop_base_response(payload)
        if isinstance(payload, dict) and isinstance(payload.get("summary"), dict):
            payload = {"summary": payload["summary"]}
        return parse_receipt_detail(payload, history_year=history_year, history_month=history_month)


class CoopPurchaseHistoryAdapter:
    """Read-only adapter for Coop receipt and purchase history."""

    def __init__(
        self,
        source: CoopSource,
        account_id: str,
        *,
        page_size: int = 20,
        max_pages: int = 50,
    ) -> None:
        trimmed = account_id.strip()
        if not trimmed or trimmed != account_id:
            raise ValueError("Coop account_id must be non-empty and trimmed")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        if max_pages < 1 or max_pages > 50:
            raise ValueError("max_pages must be between 1 and 50")

        self.source = source
        self.account_id = account_id
        self.page_size = page_size
        self.max_pages = max_pages
        self._scan_purchase_snapshots: dict[str, list[CoopPurchaseStub]] = {}

    def status(self) -> CoopAdapterStatus:
        return CoopAdapterStatus()

    def fetch_page(self, account_id: str, cursor: str | None) -> ProviderPage:
        if account_id != self.account_id:
            raise ValueError("Coop history adapter account mismatch")

        scan = _decode_coop_cursor(cursor) if cursor is not None else None
        if scan is not None and scan.account_id != account_id:
            raise ValueError("Coop history cursor account mismatch")

        if (
            scan is None
            or scan.state != "scanning"
            or scan.scan_id not in self._scan_purchase_snapshots
        ):
            scan = _CoopScanCursor(
                version=1,
                mode="full_rescan",
                account_id=account_id,
                scan_id=str(uuid4()),
                next_page=1,
                state="scanning",
            )
            purchases = self.source.get_purchases(account_id)
            self._scan_purchase_snapshots[scan.scan_id] = purchases
        else:
            purchases = self._scan_purchase_snapshots[scan.scan_id]

        page_num = scan.next_page
        start_idx = (page_num - 1) * self.page_size
        end_idx = start_idx + self.page_size
        page_purchases = purchases[start_idx:end_idx]

        has_more_records = end_idx < len(purchases)
        truncated = page_num == self.max_pages and has_more_records
        has_more = has_more_records and not truncated

        records: list[ProviderOrderRecord] = []
        for p in page_purchases:
            summary_id = p.summary_id
            if summary_id:
                receipt = self.source.get_receipt(
                    account_id,
                    summary_id,
                    history_year=p.history_year,
                    history_month=p.history_month,
                )
                if receipt is not None:
                    records.append(to_provider_order_record(receipt))

        next_cursor = _encode_coop_cursor(
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
            f"Coop history scan truncated at page {self.max_pages}; backfill is incomplete; "
            f"available records captured and next poll restarts at page 1"
            if truncated
            else None
        )

        if not has_more:
            self._scan_purchase_snapshots.pop(scan.scan_id, None)

        return ProviderPage(
            records=records,
            next_cursor=next_cursor,
            has_more=has_more,
            incomplete_reason=incomplete_reason,
        )
