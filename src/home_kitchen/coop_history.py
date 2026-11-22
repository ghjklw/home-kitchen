"""Read-only normalization for documented Coop purchase history payloads.

The mapping is based on the reverse-engineered Coop Android 4.17.3 schema.
It does not authenticate, fetch provider data, or assert purchase completion.
"""

from __future__ import annotations

import hashlib
import json
from base64 import b64decode, urlsafe_b64encode
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol, runtime_checkable
from uuid import UUID, uuid4

import httpx
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field

from home_kitchen.purchase_sync import ProviderOrderRecord, ProviderPage

COOP_SCHEMA_SOURCE = "https://gist.github.com/HelgeSverre/80a7f34f874336324184a0c513c2e6a2"
COOP_SCHEMA_VERSION = "4.17.3"
DEFAULT_COOP_BASE_URL = "https://coopay.coop.no"
DEFAULT_COOP_USER_AGENT = "Android(14)/Coop(4.42.0)/2.7.7"
DEFAULT_COOP_LOCALE = "nb_NO"
DEFAULT_COOP_PURCHASES_COUNT = 50
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


def _normalize_comma_decimal(value: Any) -> Any:
    """Normalize comma-formatted Coop numbers before Pydantic Decimal validation."""
    if isinstance(value, str):
        return value.replace(",", ".").strip()
    return value


type CommaDecimal = Annotated[Decimal, BeforeValidator(_normalize_comma_decimal)]


class CoopReceiptLine(BaseModel):
    model_config = ConfigDict(extra="ignore", alias_generator=_to_camel, populate_by_name=True)

    product_name: str | None = None
    ean13: str | None = None
    gtin13: str | None = None
    barcode: str | None = None
    quantity: CommaDecimal | None = None
    amount: CommaDecimal | None = None
    discount: CommaDecimal | None = None
    discount_amount_per_quantity: CommaDecimal | None = None
    unit_price: CommaDecimal | None = None


class CoopReceipt(BaseModel):
    """Typed detail receipt retaining provider IDs and nullable source fields."""

    model_config = ConfigDict(extra="ignore", alias_generator=_to_camel, populate_by_name=True)

    summary_id: str | None = None
    receipt_id: str | None = None
    purchase_date: AwareDatetime | None = None
    store_name: str | None = None
    store_id: str | None = None
    chain_id: str | None = None
    amount: CommaDecimal | None = None
    total_discount: CommaDecimal | None = None
    total_discount_formatted: str | None = None
    member_bonus: CommaDecimal | None = None
    lines: list[CoopReceiptLine] | None = None
    source_uri: str = COOP_SCHEMA_SOURCE
    source_schema_version: str = COOP_SCHEMA_VERSION
    source_updated_at: None = None
    revision_id: str


class CoopPurchaseStub(BaseModel):
    model_config = ConfigDict(extra="ignore", alias_generator=_to_camel, populate_by_name=True)

    summary_id: str | None = None
    purchase_date: AwareDatetime | None = None
    store_name: str | None = None
    store_id: str | None = None
    chain_id: str | None = None
    amount: CommaDecimal | None = None
    total_discount: CommaDecimal | None = None


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


def to_provider_order_record(receipt: CoopReceipt) -> ProviderOrderRecord:
    """Convert one normalized detail snapshot into the neutral immutable record.

    Coop's documented schema has no source update timestamp. The ingestion hash
    distinguishes changed detail snapshots, while observed_at is recorded by the
    neutral persistence layer.
    """
    external_id = receipt.summary_id or receipt.receipt_id
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
    "The continuation response schema is undocumented, and no source update timestamp is reported."
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

    def get_receipt(self, account_id: str, summary_id: str) -> CoopReceipt | None: ...


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

    def get_receipt(self, account_id: str, summary_id: str) -> CoopReceipt | None:
        self.receipt_calls.append(summary_id)
        if self._failure is not None:
            raise self._failure
        raw = self._receipts.get(summary_id)
        if raw is None:
            return None
        if isinstance(raw, CoopReceipt):
            return raw
        return parse_receipt_detail(raw)


def _extract_coop_purchase_stubs(data: Any) -> list[CoopPurchaseStub]:
    raw_list: list[Any]
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        purchases = data.get("purchases")
        items = data.get("items")
        monthly = data.get("monthlyBreakdown")
        if purchases is not None and not isinstance(purchases, list):
            raise UnsupportedCoopContract("Coop purchase list response has unexpected shape")
        if items is not None and not isinstance(items, list):
            raise UnsupportedCoopContract("Coop purchase list response has unexpected shape")
        if monthly is not None and not isinstance(monthly, list):
            raise UnsupportedCoopContract("Coop purchase list response has unexpected shape")
        if purchases:
            raw_list = purchases
        elif items:
            raw_list = items
        elif monthly:
            raw_list = []
            for entry in monthly:
                if not isinstance(entry, dict):
                    continue
                if isinstance(entry.get("purchases"), list):
                    raw_list.extend(entry["purchases"])
                elif isinstance(entry.get("items"), list):
                    raw_list.extend(entry["items"])
                elif "summaryId" in entry or "summary_id" in entry:
                    raw_list.append(entry)
        else:
            raw_list = []
    else:
        raise UnsupportedCoopContract("Coop purchase list response has unexpected shape")
    return [CoopPurchaseStub.model_validate(item) for item in raw_list]


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
    ) -> None:
        clean_token = access_token.strip() if isinstance(access_token, str) else ""
        clean_cookie = session_cookie.strip() if isinstance(session_cookie, str) else ""
        if not clean_token and not clean_cookie:
            raise CoopAuthenticationError("Coop-tilgangstoken eller informasjonskapsel mangler.")
        clean_device_id = device_id.strip() if isinstance(device_id, str) else ""
        if not clean_device_id:
            seed = clean_token or clean_cookie
            clean_device_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        self.access_token = clean_token or None
        self.session_cookie = clean_cookie or None
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.locale = locale
        self.device_id = clean_device_id
        self.transport = transport

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

    def _parse_purchases_response(self, resp: httpx.Response) -> list[CoopPurchaseStub]:
        self._raise_for_response_status(resp)
        payload = resp.json()
        _check_coop_base_response(payload)
        return _extract_coop_purchase_stubs(payload)

    def get_purchases(self, account_id: str) -> list[CoopPurchaseStub]:
        now = datetime.now(UTC)
        with self._client() as client:
            resp = client.get(
                f"{self.base_url}/user/pay/history/list",
                params={"n": str(DEFAULT_COOP_PURCHASES_COUNT)},
            )
            if resp.status_code < 400:
                return self._parse_purchases_response(resp)

            if resp.status_code == 404:
                resp = client.get(
                    f"{self.base_url}/user/pay/history/search",
                    params={"text": ""},
                )
            if resp.status_code != 404:
                return self._parse_purchases_response(resp)

            month_resp = client.get(
                f"{self.base_url}/user/pay/history/month",
                params={"year": str(now.year), "month": str(now.month)},
            )
            self._raise_for_response_status(month_resp, allow_404=True)
            month_stubs: list[CoopPurchaseStub] | None = None
            if month_resp.status_code < 400:
                month_payload = month_resp.json()
                _check_coop_base_response(month_payload)
                month_stubs = _extract_coop_purchase_stubs(month_payload)
                if month_stubs:
                    return month_stubs

            dash_resp = client.get(f"{self.base_url}/user/pay/history/dashboard")
            if dash_resp.status_code == 404 and month_stubs is not None:
                return month_stubs
            return self._parse_purchases_response(dash_resp)

    def get_receipt(self, account_id: str, summary_id: str) -> CoopReceipt | None:
        url = f"{self.base_url}/user/pay/history/details"
        with self._client() as client:
            resp = client.get(url, params={"summaryId": summary_id})
        if resp.status_code == 404:
            return None
        self._raise_for_response_status(resp, context=f"Coop kvittering {summary_id}")
        payload = resp.json()
        _check_coop_base_response(payload)
        if isinstance(payload, dict) and isinstance(payload.get("summary"), dict):
            return parse_receipt_detail({"summary": payload["summary"]})
        return parse_receipt_detail(payload)


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

    def status(self) -> CoopAdapterStatus:
        return CoopAdapterStatus()

    def fetch_page(self, account_id: str, cursor: str | None) -> ProviderPage:
        if account_id != self.account_id:
            raise ValueError("Coop history adapter account mismatch")

        scan = _decode_coop_cursor(cursor) if cursor is not None else None
        if scan is not None and scan.account_id != account_id:
            raise ValueError("Coop history cursor account mismatch")

        if scan is None or scan.state != "scanning":
            scan = _CoopScanCursor(
                version=1,
                mode="full_rescan",
                account_id=account_id,
                scan_id=str(uuid4()),
                next_page=1,
                state="scanning",
            )

        purchases = self.source.get_purchases(account_id)

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
                receipt = self.source.get_receipt(account_id, summary_id)
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

        return ProviderPage(
            records=records,
            next_cursor=next_cursor,
            has_more=has_more,
            incomplete_reason=incomplete_reason,
        )
