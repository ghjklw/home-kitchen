"""Provider-neutral capture of externally observed order records.

These captures never assert a purchase or connect to household history or inventory.
Adapters return ``has_more=True`` with a page-continuation cursor until backfill is
exhausted; the final page has ``has_more=False`` and carries the durable poll cursor
for the next sync. Revision IDs are immutable: corrections need a new revision ID.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from datetime import time as dt_time
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

import httpx

if TYPE_CHECKING:
    from home_kitchen.connector_ports import ConnectorCredentialPort
    from home_kitchen.context import PurchaseHistoryContext

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

_LOG = logging.getLogger(__name__)
DEFAULT_PURCHASE_SYNC_INTERVAL_SECONDS = 10800.0


class CheckpointConflict(RuntimeError):
    """Another sync advanced this provider/account since this page was fetched."""


class IncompleteScanError(RuntimeError):
    """Available source records were saved, but the provider scan was truncated."""

    def __init__(self, message: str, records_synced: int = 0) -> None:
        super().__init__(message)
        self.records_synced = records_synced


class RevisionConflict(ValueError):
    """A revision identity was replayed with different source contents."""


class ProviderOrderRecord(BaseModel):
    """One immutable provider revision with an opaque, provider-owned payload."""

    model_config = ConfigDict(extra="forbid")

    external_id: str
    revision_id: str
    source_updated_at: datetime | None = None
    payload: dict[str, object]

    @field_validator("external_id", "revision_id")
    @classmethod
    def identity_must_be_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("external identity fields must be non-empty")
        return value

    @field_validator("source_updated_at")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source_updated_at must include a timezone")
        return value.astimezone(UTC)


class ProviderPage(BaseModel):
    """A page and its cursor; terminal pages still carry a durable poll cursor."""

    records: Sequence[ProviderOrderRecord]
    next_cursor: str
    has_more: bool
    incomplete_reason: str | None = None

    @model_validator(mode="after")
    def incomplete_scan_must_be_terminal(self) -> ProviderPage:
        if self.incomplete_reason is not None and (
            self.has_more or not self.incomplete_reason.strip()
        ):
            raise ValueError("incomplete scans need a terminal cursor and a non-empty reason")
        return self

    @field_validator("next_cursor")
    @classmethod
    def cursor_must_be_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("next_cursor must be non-empty")
        return value


class ProviderAdapter(Protocol):
    """Fixture-testable page source; it performs no persistence itself."""

    def fetch_page(self, account_id: str, cursor: str | None) -> ProviderPage: ...


class ReceiptIdentityResolver(Protocol):
    """Port for resolving canonical product and unit aliases on receipt lines."""

    def resolve(self, kind: str, locale: str, context: str, alias: str) -> Any: ...


class ReceiptMappingPort(Protocol):
    """Port for asynchronous mapping of unmapped receipt line items."""

    async def map_unmapped_items(
        self,
        items: list[dict[str, Any]],
        *,
        settings: Any,
    ) -> dict[str, int]: ...


class PurchaseOrderStore(Protocol):
    """Port for durable provider order staging and checkpoint persistence."""

    def get_checkpoint(self, provider: str, account_id: str) -> str | None: ...

    def persist_page(
        self,
        provider: str,
        account_id: str,
        page: ProviderPage,
        *,
        expected_cursor: str | None,
    ) -> None: ...

    def list_records(self, provider: str | None = None) -> list[dict[str, Any]]: ...

    def list_effective_source_rows(
        self, provider: str | None = None
    ) -> Sequence[Mapping[str, Any]]: ...

    def get_provider_freshness_rows(
        self, provider: str, account_ids: Sequence[str]
    ) -> tuple[list[datetime | None], Sequence[Mapping[str, Any]]]: ...


def _identity(provider: str, account_id: str) -> tuple[str, str]:
    provider, account_id = provider.strip(), account_id.strip()
    if not provider or not account_id:
        raise ValueError("provider and account_id must be non-empty")
    return provider, account_id


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _resolve_order_store(engine_or_store: Any) -> PurchaseOrderStore:
    if callable(getattr(engine_or_store, "get_checkpoint", None)) and callable(
        getattr(engine_or_store, "persist_page", None)
    ):
        return engine_or_store
    from home_kitchen.bootstrap import create_purchase_order_store

    return create_purchase_order_store(engine_or_store)


def extract_receipt_lines(record: ProviderOrderRecord) -> list[dict[str, Any]]:
    payload = record.payload
    if not isinstance(payload, dict):
        return []
    for key in ("lines", "varelinjer", "items", "receiptLines"):
        val = payload.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
    for parent_key in ("summary", "receipt", "receiptDetails"):
        parent = payload.get(parent_key)
        if isinstance(parent, dict):
            for key in ("lines", "varelinjer", "items", "receiptLines"):
                val = parent.get(key)
                if isinstance(val, list):
                    return [item for item in val if isinstance(item, dict)]
    return []


def extract_line_ean(line: dict[str, Any]) -> str | None:
    for key in ("ean", "ean13", "gtin", "gtin13", "barcode", "varekode"):
        val = line.get(key)
        if val is not None:
            clean = str(val).strip()
            if not clean or clean == "$undefined":
                continue
            digits = re.sub(r"\D", "", clean)
            if len(digits) >= 8:
                return digits
            return clean
    return None


def extract_line_name(line: dict[str, Any]) -> str:
    for key in (
        "name",
        "product_name",
        "productName",
        "produktNavn",
        "produkt_navn",
        "produktBeskrivelse",
        "produkt_beskrivelse",
        "vareTekst",
        "description",
        "item_name",
        "title",
    ):
        val = line.get(key)
        if val is not None:
            clean = str(val).strip()
            if clean and clean != "$undefined":
                return clean
    return ""


def extract_line_unit(line: dict[str, Any]) -> str | None:
    for key in ("unit", "enhet", "enhetsType", "enhets_type", "unit_type", "unitOfMeasure"):
        val = line.get(key)
        if val is not None:
            clean = str(val).strip()
            if clean and clean != "$undefined":
                return clean
    return None


def _exception_has_class_name(exc: Exception, names: set[str]) -> bool:
    return any(cls.__name__ in names for cls in type(exc).__mro__)


def _is_kassal_rate_limit_error(exc: Exception) -> bool:
    if _exception_has_class_name(exc, {"KassalRateLimitError"}):
        return True
    if getattr(exc, "status_code", None) == 429:
        return True
    msg = str(exc).casefold()
    return "http 429" in msg or "too many attempts" in msg or "rate limit" in msg


def _serialize_kassal_product(product: Any) -> dict[str, Any]:
    return {
        "ean": getattr(product, "ean", None),
        "name": getattr(product, "name", None),
        "brand": getattr(product, "brand", None),
        "vendor": getattr(product, "vendor", None),
        "weight": getattr(product, "weight", None),
        "weight_unit": getattr(product, "weight_unit", None),
        "category": getattr(product, "category", None),
        "image": getattr(product, "image", None),
    }


def _deserialize_kassal_product(payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        ean=payload.get("ean"),
        name=payload.get("name"),
        brand=payload.get("brand"),
        vendor=payload.get("vendor"),
        weight=payload.get("weight"),
        weight_unit=payload.get("weight_unit"),
        category=payload.get("category"),
        image=payload.get("image"),
    )


class _KassalLookupSession:
    """Per-sync EAN lookup cache, DB cache bridge, and circuit breaker for Kassal.app."""

    def __init__(self, client: Any | None, persistent_cache: Any | None = None) -> None:
        self._client = client
        self._persistent_cache = persistent_cache
        self._cache: dict[str, Any] = {}
        self.rate_limited: bool = False
        self.disabled: bool = False

    def _get_persistent(self, cleaned_ean: str) -> tuple[bool, Any | None] | None:
        getter = getattr(self._persistent_cache, "get_cached_kassal_ean", None)
        if not callable(getter):
            return None
        try:
            entry = getter(cleaned_ean)
        except Exception:
            _LOG.warning("Failed reading Kassal EAN cache for %s", cleaned_ean, exc_info=True)
            return None
        if entry is None:
            return None
        found, payload = entry
        if found and isinstance(payload, dict):
            return True, _deserialize_kassal_product(payload)
        return False, None

    def _put_persistent(self, cleaned_ean: str, *, found: bool, product: Any | None) -> None:
        setter = getattr(self._persistent_cache, "put_cached_kassal_ean", None)
        if not callable(setter):
            return
        payload = _serialize_kassal_product(product) if (found and product is not None) else None
        try:
            setter(cleaned_ean, found=found, product_payload=payload)
        except Exception:
            _LOG.warning("Failed persisting Kassal EAN cache for %s", cleaned_ean, exc_info=True)

    def lookup_ean(self, ean: str | None) -> Any | None:
        if not ean:
            return None
        cleaned = re.sub(r"\s+", "", str(ean).strip())
        if not cleaned:
            return None
        if cleaned in self._cache:
            return self._cache[cleaned]

        persistent_hit = self._get_persistent(cleaned)
        if persistent_hit is not None:
            _, cached_product = persistent_hit
            self._cache[cleaned] = cached_product
            return cached_product

        if self.disabled or self._client is None:
            return None

        try:
            product = self._client.lookup_ean(cleaned)
        except Exception as exc:
            if _is_kassal_rate_limit_error(exc):
                self.rate_limited = True
                self.disabled = True
                _LOG.warning(
                    "Kassal.app rate limit reached during EAN %s lookup; "
                    "skipping remaining Kassal lookups for this sync: %s",
                    cleaned,
                    exc,
                )
                return None
            if _exception_has_class_name(exc, {"KassalAuthError"}) or getattr(
                exc, "status_code", None
            ) in (401, 403):
                self.disabled = True
                _LOG.warning(
                    "Kassal.app authentication failed during EAN %s lookup; "
                    "skipping remaining Kassal lookups for this sync: %s",
                    cleaned,
                    exc,
                )
                return None
            if _exception_has_class_name(exc, {"KassalError"}) or isinstance(exc, httpx.HTTPError):
                _LOG.warning(
                    "Kassal.app lookup failed for EAN %s; preserving receipt line data: %s",
                    cleaned,
                    exc,
                )
                self._cache[cleaned] = None
                return None
            raise

        self._put_persistent(cleaned, found=product is not None, product=product)
        self._cache[cleaned] = product
        return product


def _payload_matches_existing_ignoring_enrichment(
    incoming: dict[str, Any],
    existing: dict[str, Any],
) -> bool:
    if existing == incoming:
        return True
    cleaned_existing = copy.deepcopy(existing)
    incoming_lines = extract_receipt_lines_from_payload(incoming)
    existing_lines = extract_receipt_lines_from_payload(cleaned_existing)
    if len(incoming_lines) != len(existing_lines):
        return False
    for inc_line, ext_line in zip(incoming_lines, existing_lines, strict=True):
        kassal_info = ext_line.pop("kassal", None)
        ext_line.pop("canonical_product_id", None)
        ext_line.pop("canonical_unit_id", None)
        if isinstance(kassal_info, dict) and ext_line.get("unit") == kassal_info.get("weight_unit"):
            if "unit" not in inc_line:
                ext_line.pop("unit", None)
            elif inc_line.get("unit") is None:
                ext_line["unit"] = None
    return cleaned_existing == incoming


def enrich_and_map_receipt_record(
    record: ProviderOrderRecord,
    kassal_client: Any | None,
    identity_service: ReceiptIdentityResolver | None = None,
) -> list[dict[str, Any]]:
    """Enrich barcode lines via Kassal.app and resolve canonical product/unit identities.

    Returns unmapped line items that require async background AI mapping.
    """
    lines = extract_receipt_lines(record)
    if not lines:
        return []

    kassal_session: _KassalLookupSession | None = (
        kassal_client
        if isinstance(kassal_client, _KassalLookupSession)
        else (_KassalLookupSession(kassal_client) if kassal_client is not None else None)
    )

    unmapped: list[dict[str, Any]] = []

    for line in lines:
        ean = extract_line_ean(line)
        kassal_prod = None
        if ean and kassal_session is not None:
            kassal_prod = kassal_session.lookup_ean(ean)
            if kassal_prod is not None:
                line["kassal"] = {
                    "ean": kassal_prod.ean,
                    "name": kassal_prod.name,
                    "brand": kassal_prod.brand,
                    "vendor": kassal_prod.vendor,
                    "weight": kassal_prod.weight,
                    "weight_unit": kassal_prod.weight_unit,
                    "category": kassal_prod.category,
                    "image": kassal_prod.image,
                }
                if not line.get("unit") and kassal_prod.weight_unit:
                    line["unit"] = kassal_prod.weight_unit

        raw_name = extract_line_name(line)
        candidate_names: list[str] = []
        if kassal_prod and kassal_prod.name:
            candidate_names.append(kassal_prod.name)
        if raw_name and raw_name not in candidate_names:
            candidate_names.append(raw_name)

        candidate_unit = extract_line_unit(line)
        if not candidate_unit and kassal_prod and kassal_prod.weight_unit:
            candidate_unit = kassal_prod.weight_unit

        prod_id: UUID | None = None
        unit_id: UUID | None = None

        if identity_service is not None:
            from home_kitchen.catalogue.identity import candidate_identity_names

            for name in candidate_names:
                for candidate in candidate_identity_names(name):
                    for locale in ("nb", "en", "fr"):
                        for ctx in ("receipt", "recipe"):
                            res = identity_service.resolve("product", locale, ctx, candidate)
                            if res.status == "resolved" and len(res.candidates) == 1:
                                prod_id = res.candidates[0].id
                                break
                        if prod_id is not None:
                            break
                    if prod_id is not None:
                        break
                if prod_id is not None:
                    break

            if candidate_unit:
                for locale in ("nb", "en", "fr"):
                    for ctx in ("receipt", "recipe"):
                        res = identity_service.resolve("unit", locale, ctx, candidate_unit)
                        if res.status == "resolved" and len(res.candidates) == 1:
                            unit_id = res.candidates[0].id
                            break
                    if unit_id is not None:
                        break

        if prod_id is not None:
            line["canonical_product_id"] = str(prod_id)
        if unit_id is not None:
            line["canonical_unit_id"] = str(unit_id)

        if line.get("canonical_product_id") is None and candidate_names:
            unmapped.append(
                {
                    "line": line,
                    "name": candidate_names[0],
                    "unit": candidate_unit,
                    "ean": ean,
                }
            )

    return unmapped


def sync_account(
    engine_or_store: PurchaseOrderStore | Any,
    provider: str,
    account_id: str,
    adapter: ProviderAdapter,
    *,
    kassal_client: Any | None = None,
    kassal_client_factory: Callable[[str], Any] | None = None,
    identity_service: ReceiptIdentityResolver | None = None,
    settings: Any | None = None,
    connector_store: ConnectorCredentialPort | None = None,
    household_id: UUID | None = None,
    on_unmapped_lines: Callable[[list[dict[str, Any]]], None] | None = None,
) -> int:
    """Fetch pages; terminal-page cursor is saved for the next provider poll."""
    provider, account_id = _identity(provider, account_id)
    order_store = _resolve_order_store(engine_or_store)
    existing_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    if callable(getattr(order_store, "list_records", None)):
        for row in order_store.list_records(provider=provider):
            if row.get("account_id") == account_id and isinstance(row.get("payload"), dict):
                existing_by_key[(str(row["external_id"]), str(row["revision_id"]))] = row["payload"]

    kassal_session: _KassalLookupSession | None = (
        kassal_client
        if isinstance(kassal_client, _KassalLookupSession)
        else (
            _KassalLookupSession(kassal_client, persistent_cache=order_store)
            if kassal_client is not None
            else None
        )
    )

    cursor = order_store.get_checkpoint(provider, account_id)
    seen: set[str | None] = set()
    count = 0
    while cursor not in seen:
        seen.add(cursor)
        page = adapter.fetch_page(account_id, cursor)
        if page.has_more and page.next_cursor == cursor:
            raise ValueError("provider returned a non-advancing continuation cursor")
        if page.has_more and page.next_cursor in seen:
            raise ValueError("provider returned a repeated continuation cursor")

        records_to_enrich: list[ProviderOrderRecord] = []
        for rec in page.records:
            existing_payload = existing_by_key.get((rec.external_id, rec.revision_id))
            if existing_payload is not None:
                incoming_json = rec.model_dump(mode="json")["payload"]
                if _payload_matches_existing_ignoring_enrichment(incoming_json, existing_payload):
                    rec.payload = copy.deepcopy(existing_payload)
                    continue
            records_to_enrich.append(rec)

        eans_on_page = {
            re.sub(r"\s+", "", ean.strip())
            for rec in records_to_enrich
            for line in extract_receipt_lines(rec)
            if (ean := extract_line_ean(line))
        }
        has_uncached_eans = False
        if eans_on_page and kassal_session is None:
            cache_getter = getattr(order_store, "get_cached_kassal_ean", None)
            if callable(cache_getter):
                has_uncached_eans = any(cache_getter(ean) is None for ean in eans_on_page)
            else:
                has_uncached_eans = True
        if eans_on_page and kassal_session is None:
            if not has_uncached_eans:
                kassal_session = _KassalLookupSession(None, persistent_cache=order_store)
            else:
                active_kassal = None
                api_key: str | None = None
                if connector_store is not None and household_id is not None:
                    creds = connector_store.get_credentials(household_id, "kassal")
                    if creds and isinstance(creds, dict) and creds.get("api_key"):
                        api_key = str(creds["api_key"])
                if (
                    api_key is None
                    and settings is not None
                    and getattr(settings, "kassal_api_key", None)
                ):
                    s_key = settings.kassal_api_key
                    clean_k = (
                        s_key.get_secret_value()
                        if hasattr(s_key, "get_secret_value")
                        else str(s_key)
                    )
                    if clean_k.strip():
                        api_key = clean_k.strip()
                if api_key is not None:
                    if kassal_client_factory is not None:
                        active_kassal = kassal_client_factory(api_key)
                    else:
                        from home_kitchen.bootstrap import create_kassal_client

                        active_kassal = create_kassal_client(api_key)
                if active_kassal is None:
                    from home_kitchen.bootstrap import missing_kassal_key_error

                    raise ValueError(missing_kassal_key_error())
                kassal_session = _KassalLookupSession(active_kassal, persistent_cache=order_store)

        all_unmapped: list[dict[str, Any]] = []
        for rec in records_to_enrich:
            unmapped = enrich_and_map_receipt_record(rec, kassal_session, identity_service)
            if unmapped:
                all_unmapped.extend(unmapped)

        order_store.persist_page(provider, account_id, page, expected_cursor=cursor)
        for rec in page.records:
            existing_by_key[(rec.external_id, rec.revision_id)] = rec.model_dump(mode="json")[
                "payload"
            ]
        count += len(page.records)

        if all_unmapped and callable(on_unmapped_lines):
            on_unmapped_lines(all_unmapped)

        if page.incomplete_reason is not None:
            # Persist first: the terminal cursor allows the next poll to restart,
            # while the typed failure prevents claiming a completed backfill.
            raise IncompleteScanError(page.incomplete_reason, records_synced=count)
        if not page.has_more:
            return count
        cursor = page.next_cursor
    raise ValueError("provider returned a repeated continuation cursor")


SUPPORTED_PROVIDERS: tuple[str, ...] = ("oda", "coop", "trumf", "rema1000")

RecordLifecycle = Literal[
    "purchase",
    "correction",
    "return",
    "cancellation",
    "non_receipt_stub",
    "pending_order",
]

_CANCELLATION_STATUSES = frozenset(
    {"cancelled", "canceled", "failed", "void", "voided", "annullert"}
)
_RETURN_STATUSES = frozenset(
    {
        "return",
        "returned",
        "refund",
        "refunded",
        "credit_note",
        "retur",
        "kreditnota",
        "refund_scenario",
    }
)
_DELIVERED_STATUSES = frozenset({"delivered", "completed", "paid", "settled"})


class NormalizedOrderLine(BaseModel):
    """Normalized receipt/order line item with explicit return/adjustment semantics."""

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


class EffectiveProviderOrder(BaseModel):
    """Resolved latest state of a provider order/receipt after corrections and returns."""

    model_config = ConfigDict(extra="forbid")

    source_id: str | None = None
    provider: str
    account_id: str
    external_id: str
    revision_id: str
    lifecycle: RecordLifecycle
    is_correction: bool = False
    is_return: bool = False
    is_cancellation: bool = False
    is_active_purchase: bool = True
    provenance: Literal["provider_verified", "user_confirmed", "uncertain"] = "provider_verified"
    returned_external_id: str | None = None
    revision_count: int = 1
    superseded_revision_ids: list[str] = Field(default_factory=list)
    total_amount: Decimal | None = None
    transaction_at: AwareDatetime | None = None
    source_updated_at: AwareDatetime | None = None
    observed_at: AwareDatetime | None = None
    store_name: str | None = None
    evidence_ref: str | None = None
    lines: list[NormalizedOrderLine] = Field(default_factory=list)
    returned_lines: list[NormalizedOrderLine] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


def _to_decimal(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        dec = Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return dec if dec.is_finite() else None


def _envelope_for_provider(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    if provider == "coop" and isinstance(payload.get("summary"), dict):
        return payload["summary"]
    if provider == "rema1000" and isinstance(payload.get("receipt"), dict):
        return payload["receipt"]
    if provider == "oda" and isinstance(payload.get("order"), dict):
        return payload["order"]
    return payload


def extract_returned_external_id(payload: dict[str, Any]) -> str | None:
    """Extract referenced original order/receipt external_id for standalone return/refund records."""
    candidates: list[dict[str, Any]] = [payload]
    for parent_key in ("summary", "receipt", "order", "receiptDetails"):
        sub = payload.get(parent_key)
        if isinstance(sub, dict):
            candidates.append(sub)
    for container in candidates:
        for key in (
            "returnedExternalId",
            "returned_external_id",
            "originalReceiptId",
            "original_receipt_id",
            "originalOrderId",
            "original_order_id",
            "originalOrderNumber",
            "original_order_number",
            "creditedReceiptId",
            "credited_receipt_id",
            "returnFor",
            "return_for",
            "return_for_external_id",
            "relatedReceiptId",
            "related_receipt_id",
        ):
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def extract_record_total_amount(provider: str, payload: dict[str, Any]) -> Decimal | None:
    """Extract total monetary amount across provider payload envelopes."""
    env = _envelope_for_provider(provider, payload)
    for container in (env, payload):
        for key in (
            "belop",
            "amount",
            "totalAmount",
            "total_amount",
            "grossAmount",
            "gross_amount",
            "total",
            "sum",
        ):
            dec = _to_decimal(container.get(key))
            if dec is not None:
                return dec
    return None


def extract_record_store_name(provider: str, payload: dict[str, Any]) -> str | None:
    """Extract store or chain attribution from a provider payload."""
    env = _envelope_for_provider(provider, payload)
    for container in (env, payload):
        for key in ("storeName", "store_name", "beskrivelse", "chain", "chainId"):
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    defaults = {"oda": "Oda", "coop": "Coop", "rema1000": "REMA 1000", "trumf": "Trumf"}
    return defaults.get(provider)


def extract_record_evidence_ref(provider: str, payload: dict[str, Any]) -> str | None:
    """Extract provider receipt or order reference ID."""
    env = _envelope_for_provider(provider, payload)
    for container in (env, payload):
        for key in ("kvitteringsId", "receiptId", "orderNumber", "orderId", "batchId", "summaryId"):
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def is_non_receipt_stub(provider: str, payload: dict[str, Any]) -> bool:
    """Return True when the provider snapshot explicitly lacks digital receipt details."""
    env = _envelope_for_provider(provider, payload)
    if provider == "trumf":
        for container in (payload, env):
            for key in ("hasReceipt", "harKvittering"):
                if key in container and not bool(container.get(key)):
                    return True
        return not (
            bool(payload.get("kvitteringsId")) or bool(extract_receipt_lines_from_payload(payload))
        )
    if provider == "rema1000":
        for container in (env, payload):
            for key in ("hasReceipt", "harKvittering"):
                if key in container:
                    return not bool(container.get(key))
        return not bool(extract_receipt_lines_from_payload(payload))
    if provider == "coop":
        lines = extract_receipt_lines_from_payload(payload)
        receipt_id = env.get("receiptId") or payload.get("receiptId")
        return not (bool(receipt_id) and bool(lines))
    return False


def extract_receipt_lines_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract raw line dicts from any supported provider payload dict."""
    if not isinstance(payload, dict):
        return []
    for key in ("lines", "varelinjer", "items", "receiptLines"):
        val = payload.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
    for parent_key in ("summary", "receipt", "receiptDetails"):
        parent = payload.get(parent_key)
        if isinstance(parent, dict):
            for key in ("lines", "varelinjer", "items", "receiptLines"):
                val = parent.get(key)
                if isinstance(val, list):
                    return [item for item in val if isinstance(item, dict)]
    return []


def extract_normalized_lines(provider: str, payload: dict[str, Any]) -> list[NormalizedOrderLine]:
    """Normalize provider-specific receipt or order lines including return markers."""
    normalized: list[NormalizedOrderLine] = []
    if provider == "oda" and isinstance(payload.get("order"), dict):
        order_obj = payload["order"]
        for item in order_obj.get("products") or []:
            if not isinstance(item, dict):
                continue
            prod_obj = item.get("product") or {}
            if not isinstance(prod_obj, dict):
                prod_obj = {}
            name = str(prod_obj.get("name") or item.get("name") or "").strip()
            qty = _to_decimal(item.get("quantity"))
            amt = _to_decimal(item.get("totalGrossAmount") or item.get("amount"))
            is_ret = bool(
                item.get("isReturn")
                or item.get("is_return")
                or item.get("returned")
                or (qty is not None and qty < 0)
                or (amt is not None and amt < 0)
            )
            oda_pid = prod_obj.get("id") or item.get("oda_product_id")
            canon_pid = item.get("canonical_product_id") or prod_obj.get("canonical_product_id")
            canon_uid = item.get("canonical_unit_id") or prod_obj.get("canonical_unit_id")
            raw_cat = prod_obj.get("category") or item.get("category")
            if (
                not raw_cat
                and isinstance(prod_obj.get("categories"), list)
                and prod_obj["categories"]
            ):
                first_cat = prod_obj["categories"][0]
                raw_cat = first_cat.get("name") if isinstance(first_cat, dict) else first_cat
            cat_str = str(raw_cat).strip() if isinstance(raw_cat, str) and raw_cat.strip() else None
            normalized.append(
                NormalizedOrderLine(
                    name=name or (f"Oda SKU {oda_pid}" if oda_pid is not None else "Oda item"),
                    quantity=qty,
                    unit=(
                        str(prod_obj.get("unitName")).strip() if prod_obj.get("unitName") else None
                    ),
                    amount=amt,
                    ean=extract_line_ean(item),
                    canonical_product_id=str(canon_pid) if canon_pid else None,
                    canonical_unit_id=str(canon_uid) if canon_uid else None,
                    oda_product_id=str(oda_pid) if oda_pid is not None else None,
                    category=cat_str,
                    is_return=is_ret,
                )
            )
        return normalized

    raw_lines = extract_receipt_lines_from_payload(payload)
    for line in raw_lines:
        raw_kassal = line.get("kassal")
        kassal: dict[str, Any] = raw_kassal if isinstance(raw_kassal, dict) else {}
        raw_name = extract_line_name(line)
        kassal_name = str(kassal.get("name") or "").strip()
        display_name = kassal_name or raw_name or str(line.get("sourceName") or "").strip()
        qty = _to_decimal(line.get("quantity") if "quantity" in line else line.get("antall"))
        amt = _to_decimal(
            line.get("amount")
            if "amount" in line
            else (line.get("belop") if "belop" in line else line.get("total"))
        )
        unit = extract_line_unit(line)
        if not unit and kassal.get("weight_unit"):
            unit = str(kassal["weight_unit"]).strip() or None
        kassal_ean = kassal.get("ean")
        ean = extract_line_ean(line) or (str(kassal_ean).strip() if kassal_ean else None)
        oda_pid = line.get("oda_product_id") or line.get("product_id") or line.get("sku")
        canon_pid = line.get("canonical_product_id")
        canon_uid = line.get("canonical_unit_id")
        raw_cat = (
            kassal.get("category")
            or line.get("category")
            or line.get("varegruppe")
            or line.get("categoryName")
        )
        cat_str = str(raw_cat).strip() if isinstance(raw_cat, str) and raw_cat.strip() else None
        line_type = str(line.get("type") or line.get("lineType") or "").strip().casefold()
        is_ret = bool(
            line.get("isReturn")
            or line.get("is_return")
            or line.get("returned")
            or line_type in _RETURN_STATUSES
            or (qty is not None and qty < 0)
            or (amt is not None and amt < 0)
        )
        normalized.append(
            NormalizedOrderLine(
                name=display_name or "Unknown item",
                raw_name=raw_name or str(line.get("sourceName") or "").strip() or None,
                quantity=qty,
                unit=unit,
                amount=amt,
                ean=ean,
                canonical_product_id=str(canon_pid) if canon_pid else None,
                canonical_unit_id=str(canon_uid) if canon_uid else None,
                oda_product_id=str(oda_pid) if oda_pid is not None else None,
                category=cat_str,
                is_return=is_ret,
            )
        )
    return normalized


def classify_record_lifecycle(
    provider: str,
    payload: dict[str, Any],
    *,
    revision_count: int = 1,
    lines: Sequence[NormalizedOrderLine] | None = None,
) -> tuple[RecordLifecycle, bool, bool, bool]:
    """Classify a provider snapshot into lifecycle, correction, return, and cancellation flags."""
    p = provider.strip().lower()
    env = _envelope_for_provider(p, payload)
    raw_tracking = payload.get("tracking")
    tracking: dict[str, Any] = raw_tracking if isinstance(raw_tracking, dict) else {}

    raw_statuses: list[str] = []
    for container in (tracking, env, payload):
        for key in ("status", "lifecycle", "transactionType", "transaksjonsKategori", "type"):
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                raw_statuses.append(val.strip().casefold())

    is_correction = revision_count > 1

    cancelled_flag = any(
        bool(container.get(flag))
        for container in (tracking, env, payload)
        for flag in ("isCancelled", "is_cancelled", "cancelled", "canceled")
    )
    if cancelled_flag or any(st in _CANCELLATION_STATUSES for st in raw_statuses):
        return ("cancellation", is_correction, False, True)

    returned_flag = any(
        bool(container.get(flag))
        for container in (tracking, env, payload)
        for flag in ("isReturn", "is_return", "isReturned", "isRefund", "is_refund", "refunded")
    )
    total_amount = extract_record_total_amount(p, payload)
    returned_ext = extract_returned_external_id(payload)
    parsed_lines = list(lines) if lines is not None else extract_normalized_lines(p, payload)
    all_lines_returned = bool(parsed_lines) and all(line.is_return for line in parsed_lines)

    if (
        returned_flag
        or any(st in _RETURN_STATUSES for st in raw_statuses)
        or (total_amount is not None and total_amount < 0)
        or returned_ext is not None
        or all_lines_returned
    ):
        return ("return", is_correction, True, False)

    if p == "oda":
        if raw_statuses and not any(st in _DELIVERED_STATUSES for st in raw_statuses):
            return ("pending_order", is_correction, False, False)
        if not raw_statuses:
            return ("pending_order", is_correction, False, False)
    elif is_non_receipt_stub(p, payload):
        return ("non_receipt_stub", is_correction, False, False)

    if is_correction:
        return ("correction", True, False, False)
    return ("purchase", False, False, False)


def _line_match_key(line: NormalizedOrderLine) -> str:
    if line.canonical_product_id:
        return f"pid:{line.canonical_product_id}"
    if line.ean:
        return f"ean:{line.ean}"
    return f"name:{' '.join(line.name.casefold().split())}"


def _net_order_lines(
    lines: Sequence[NormalizedOrderLine],
) -> tuple[list[NormalizedOrderLine], list[NormalizedOrderLine]]:
    """Separate active positive lines from returned/negative lines, netting matching quantities."""
    active: list[NormalizedOrderLine] = []
    returned: list[NormalizedOrderLine] = []

    for line in lines:
        if line.is_return:
            returned.append(line)
        else:
            active.append(line.model_copy())

    if not returned or not active:
        return active, returned

    remaining_active = list(active)
    for ret in returned:
        ret_key = _line_match_key(ret)
        ret_qty = abs(ret.quantity) if ret.quantity is not None else None
        for idx, act in enumerate(remaining_active):
            if _line_match_key(act) != ret_key:
                continue
            if act.quantity is not None and ret_qty is not None:
                net_qty = act.quantity - ret_qty
                if net_qty <= 0:
                    ret_qty = abs(net_qty)
                    remaining_active[idx] = act.model_copy(update={"quantity": Decimal("0")})
                    if ret_qty == 0:
                        break
                else:
                    remaining_active[idx] = act.model_copy(update={"quantity": net_qty})
                    ret_qty = Decimal("0")
                    break
            else:
                remaining_active[idx] = act.model_copy(update={"quantity": Decimal("0")})
                break

    net_active = [line for line in remaining_active if line.quantity is None or line.quantity > 0]
    return net_active, returned


def resolve_effective_orders(
    rows: Sequence[Mapping[str, Any]],
    *,
    provider: str | None = None,
) -> list[EffectiveProviderOrder]:
    """Resolve immutable provider_order_sources revisions into effective orders/receipts.

    - Groups revisions by ``(provider, external_id)`` so corrections supersede earlier snapshots.
    - Orders revisions chronologically by ``(source_updated_at or observed_at, observed_at)``.
    - Nets line-level returns and linked standalone return/refund receipts against original orders.
    """
    filter_provider = provider.strip().lower() if provider is not None else None
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        prov = str(row["provider"]).strip().lower()
        if filter_provider is not None and prov != filter_provider:
            continue
        ext_id = str(row.get("external_id") or row.get("id") or "").strip()
        if not ext_id:
            continue
        grouped.setdefault((prov, ext_id), []).append(row)

    def _sort_key(row: Mapping[str, Any]) -> tuple[datetime, datetime, str]:
        obs = _utc(row.get("observed_at")) or datetime.min.replace(tzinfo=UTC)
        src = _utc(row.get("source_updated_at")) or obs
        return (src, obs, str(row.get("revision_id") or ""))

    effective_by_key: dict[tuple[str, str], EffectiveProviderOrder] = {}
    ordered_keys: list[tuple[str, str]] = []

    for key, revisions in grouped.items():
        prov, ext_id = key
        sorted_revs = sorted(revisions, key=_sort_key)
        latest = sorted_revs[-1]
        superseded: list[str] = []
        seen_rev_ids: set[str] = set()
        latest_rev_id = str(latest.get("revision_id") or "")
        for rev in sorted_revs[:-1]:
            rev_id = str(rev.get("revision_id") or "")
            if rev_id and rev_id != latest_rev_id and rev_id not in seen_rev_ids:
                seen_rev_ids.add(rev_id)
                superseded.append(rev_id)

        payload = latest.get("payload")
        payload_dict: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
        revision_count = len(superseded) + 1
        raw_lines = extract_normalized_lines(prov, payload_dict)
        lifecycle, is_correction, is_return, is_cancellation = classify_record_lifecycle(
            prov,
            payload_dict,
            revision_count=revision_count,
            lines=raw_lines,
        )
        net_lines, returned_lines = _net_order_lines(raw_lines)
        if raw_lines and not net_lines and not is_cancellation:
            is_return = True
            lifecycle = "return"

        is_active = (
            not is_cancellation
            and not is_return
            and lifecycle not in ("non_receipt_stub", "pending_order")
        )
        provenance: Literal["provider_verified", "user_confirmed", "uncertain"] = (
            "provider_verified" if is_active else "uncertain"
        )
        order = EffectiveProviderOrder(
            source_id=str(latest["id"]) if latest.get("id") is not None else None,
            provider=prov,
            account_id=str(latest.get("account_id") or "default"),
            external_id=ext_id,
            revision_id=latest_rev_id or "rev-0",
            lifecycle=lifecycle,
            is_correction=is_correction,
            is_return=is_return,
            is_cancellation=is_cancellation,
            is_active_purchase=is_active,
            provenance=provenance,
            returned_external_id=extract_returned_external_id(payload_dict),
            revision_count=revision_count,
            superseded_revision_ids=superseded,
            total_amount=extract_record_total_amount(prov, payload_dict),
            transaction_at=extract_transaction_time(prov, payload_dict),
            source_updated_at=_utc(latest.get("source_updated_at")),
            observed_at=_utc(latest.get("observed_at")),
            store_name=extract_record_store_name(prov, payload_dict),
            evidence_ref=extract_record_evidence_ref(prov, payload_dict),
            lines=net_lines,
            returned_lines=returned_lines,
            payload=payload_dict,
        )
        effective_by_key[key] = order
        ordered_keys.append(key)

    # Apply standalone linked return/refund records against their original order/receipt.
    for key in ordered_keys:
        ret_order = effective_by_key[key]
        if not ret_order.is_return or not ret_order.returned_external_id:
            continue
        target_key = (ret_order.provider, ret_order.returned_external_id)
        target = effective_by_key.get(target_key)
        if target is None or target.is_cancellation:
            continue

        ret_items = ret_order.returned_lines or ret_order.lines
        if not ret_items:
            if (
                ret_order.total_amount is None
                or target.total_amount is None
                or abs(ret_order.total_amount) >= abs(target.total_amount)
            ):
                effective_by_key[target_key] = target.model_copy(
                    update={
                        "lifecycle": "return",
                        "is_return": True,
                        "is_active_purchase": False,
                        "provenance": "uncertain",
                        "returned_lines": list(target.returned_lines) + list(target.lines),
                        "lines": [],
                    }
                )
            continue

        flipped = [item.model_copy(update={"is_return": True}) for item in ret_items]
        combined_lines = list(target.lines) + flipped
        net_active, newly_returned = _net_order_lines(combined_lines)
        all_returned = list(target.returned_lines) + newly_returned
        fully_returned = bool(target.lines) and not net_active
        effective_by_key[target_key] = target.model_copy(
            update={
                "lifecycle": "return" if fully_returned else target.lifecycle,
                "is_return": fully_returned,
                "is_active_purchase": False if fully_returned else target.is_active_purchase,
                "provenance": "uncertain" if fully_returned else target.provenance,
                "lines": net_active,
                "returned_lines": all_returned,
            }
        )

    result = list(effective_by_key.values())
    result.sort(
        key=lambda item: (
            -(item.observed_at.timestamp()) if item.observed_at else 0.0,
            -(item.source_updated_at.timestamp()) if item.source_updated_at else 0.0,
            item.provider,
            item.external_id,
        )
    )
    return result


class CoverageWindow(BaseModel):
    """Historical coverage and retention boundaries for a provider's records."""

    model_config = ConfigDict(extra="forbid")

    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    total_records: int = 0
    correction_count: int = 0
    return_count: int = 0
    cancellation_count: int = 0
    retained_months: int | None = None
    description: str | None = None


class PostingDelayInfo(BaseModel):
    """Delay between transaction/purchase event and provider posting time."""

    model_config = ConfigDict(extra="forbid")

    tracked: bool
    latest_delay_seconds: float | None = None
    description: str


class IngestionDelayInfo(BaseModel):
    """Delay between provider record availability and Kitchen ingestion."""

    model_config = ConfigDict(extra="forbid")

    tracked: bool
    latest_delay_seconds: float | None = None
    target_latency_minutes: int = 15
    description: str


class ProviderStatusInfo(BaseModel):
    """Provider-specific sync health, freshness, and connection boundaries."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    status: Literal["connected", "not_configured", "unavailable", "degraded", "error"]
    auth_state: Literal["connected", "not_configured", "expired", "unavailable", "error"]
    auth_method: str
    last_successful_sync: AwareDatetime | None = None
    last_sync_error: str | None = None
    coverage_window: CoverageWindow
    provider_posting_delay: PostingDelayInfo
    kitchen_ingestion_delay: IngestionDelayInfo
    limitations: str | None = None


class PurchaseHistoryStatusResponse(BaseModel):
    """Status reporting for all supported grocery purchase history providers."""

    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderStatusInfo]
    overall_status: Literal["connected", "not_configured", "degraded", "error"]
    checked_at: AwareDatetime


class ProviderSyncResult(BaseModel):
    """Result of running a sync pass against one provider."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    status: Literal["success", "skipped", "incomplete", "failed"]
    records_synced: int = 0
    accounts_synced: int = 0
    checkpoint: str | None = None
    duration_seconds: float = 0.0
    error: str | None = None


class PurchaseHistorySyncRequest(BaseModel):
    """Request envelope for bounded purchase history synchronization."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["oda", "coop", "trumf", "rema1000"] | None = None
    max_pages: int = Field(default=50, ge=1, le=50)


class PurchaseHistorySyncResponse(BaseModel):
    """Aggregated outcome of manual or scheduled purchase history synchronization."""

    model_config = ConfigDict(extra="forbid")

    results: dict[str, ProviderSyncResult]
    total_records_synced: int
    completed_at: AwareDatetime


def extract_transaction_time(provider: str, payload: dict[str, Any]) -> datetime | None:
    """Extract purchase/transaction timestamp from a provider-specific payload."""
    time_str: str | None = None
    if provider == "trumf":
        val = payload.get("transaksjonsTidspunkt") or payload.get("dato")
        if isinstance(val, str):
            time_str = val
    elif provider == "coop":
        summary = payload.get("summary")
        if isinstance(summary, dict):
            val = summary.get("purchaseDate")
        else:
            val = payload.get("purchaseDate")
        if isinstance(val, str):
            time_str = val
    elif provider == "oda":
        order = payload.get("order")
        if isinstance(order, dict):
            val = order.get("deliveryDate") or order.get("orderedAt")
        else:
            val = payload.get("deliveryDate") or payload.get("orderedAt")
        if isinstance(val, str):
            time_str = val
    elif provider == "rema1000":
        val = (
            payload.get("purchase_date")
            or payload.get("purchaseDate")
            or payload.get("timestamp")
            or payload.get("transaksjonsTidspunkt")
            or payload.get("transaksjons_tidspunkt")
            or payload.get("dato")
        )
        if isinstance(val, str):
            time_str = val

    if not time_str:
        return None
    try:
        if len(time_str) == 10 and time_str.count("-") == 2:
            return datetime.combine(date.fromisoformat(time_str), dt_time.min, tzinfo=UTC)
        cleaned = time_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return _utc(dt)
    except Exception:
        return None


class PurchaseHistorySyncService:
    """Unified read-only sync orchestrator and freshness reporter.

    Coordinates registered providers (Oda, Coop, Trumf, Rema 1000) using durable
    per-provider checkpoints, idempotent retries, and overlap deduplication.
    Strictly separates provider-reported order/receipt records from confirmed kitchen
    purchases and stock; never asserts kitchen stock or cooked recipes.
    """

    SUPPORTED_PROVIDERS: tuple[str, ...] = SUPPORTED_PROVIDERS

    def __init__(
        self,
        engine: Any = None,
        settings: Any = None,
        oda_service: Any = None,
        adapters: dict[str, ProviderAdapter] | None = None,
        *,
        order_store: PurchaseOrderStore | None = None,
        connector_store: ConnectorCredentialPort | None = None,
        household_id: UUID | None = None,
        adapter_factory: Callable[[str, str, dict[str, Any]], ProviderAdapter | None] | None = None,
        oda_adapter_factory: Callable[[Any, str], ProviderAdapter] | None = None,
        kassal_client_factory: Callable[[str], Any] | None = None,
        connector_auth: Any | None = None,
        identity_service: ReceiptIdentityResolver | None = None,
        receipt_mapper: ReceiptMappingPort | None = None,
        kassal_client: Any | None = None,
    ) -> None:
        self.engine = engine
        self._order_store: PurchaseOrderStore | None = (
            order_store
            if order_store is not None
            else (_resolve_order_store(engine) if engine is not None else None)
        )
        self.settings = settings
        self.oda_service = oda_service
        self.connector_store = connector_store
        self.household_id = household_id
        self.adapter_factory = adapter_factory
        self.oda_adapter_factory = oda_adapter_factory
        self.kassal_client_factory = kassal_client_factory
        self.connector_auth = connector_auth
        self._adapters: dict[str, ProviderAdapter] = dict(adapters or {})
        self._account_adapters: dict[tuple[str, str], ProviderAdapter] = {}
        self._last_sync_time: dict[str, datetime] = {}
        self._last_sync_error: dict[str, str | None] = {}
        self._last_auth_error: dict[str, str | None] = {}
        self._partial_auth_failure: dict[str, bool] = {}
        self.identity_service: ReceiptIdentityResolver | None = identity_service
        self.receipt_mapper: ReceiptMappingPort | None = receipt_mapper
        self.kassal_client: Any = kassal_client
        self._kassal_clients_by_key: dict[str, tuple[Any, Any]] = {}

    @staticmethod
    def _is_auth_exception(exc: Exception) -> bool:
        exc_name = type(exc).__name__
        if exc_name in {
            "CoopAuthenticationError",
            "Rema1000AuthenticationError",
            "TrumfAuthenticationError",
            "ConnectorAuthError",
        }:
            return True
        status_code = getattr(exc, "status_code", None)
        if exc_name == "OdaError" and status_code in (401, 403, 409):
            return True
        if exc_name == "HTTPException" and status_code in (400, 401, 403, 409):
            return True
        return False

    def _sanitize_account_error(self, provider: str, account_id: str, exc: Exception) -> str:
        raw = str(getattr(exc, "detail", None) or str(exc) or type(exc).__name__)
        if self.connector_store is not None and self.household_id is not None:
            try:
                creds = self.connector_store.get_credentials(
                    self.household_id, provider, account_id=account_id
                )
            except Exception:
                creds = None
            if isinstance(creds, dict):
                for key in (
                    "access_token",
                    "refresh_token",
                    "session_token",
                    "session_cookie",
                    "cookie",
                ):
                    secret = creds.get(key)
                    if isinstance(secret, str) and secret.strip():
                        raw = raw.replace(secret.strip(), "[REDACTED]")
        return raw

    def _record_account_auth_error(self, provider: str, account_id: str, message: str) -> None:
        if (
            self.connector_store is None
            or self.household_id is None
            or provider not in ("rema1000", "coop", "trumf")
        ):
            return
        try:
            meta = self.connector_store.get_connection_metadata(
                self.household_id, provider, account_id=account_id
            )
            if meta is None:
                return
            self.connector_store.update_account_metadata(
                self.household_id, provider, account_id, {"last_error": message}
            )
        except Exception:
            _LOG.warning(
                "Failed to persist connector auth error for %s account %s",
                provider,
                account_id,
            )

    def _clear_account_auth_error(self, provider: str, account_id: str) -> None:
        if (
            self.connector_store is None
            or self.household_id is None
            or provider not in ("rema1000", "coop", "trumf")
        ):
            return
        try:
            meta = self.connector_store.get_connection_metadata(
                self.household_id, provider, account_id=account_id
            )
            if meta is None or not isinstance(meta.metadata, dict):
                return
            if "last_error" in meta.metadata:
                self.connector_store.update_account_metadata(
                    self.household_id,
                    provider,
                    account_id,
                    {},
                    remove_keys=("last_error",),
                )
        except Exception:
            _LOG.warning(
                "Failed to clear connector auth error for %s account %s",
                provider,
                account_id,
            )

    def _dispatch_background_mapping(self, items: list[dict[str, Any]]) -> None:
        """Run AI/variant mapping for unmapped receipt lines without blocking sync."""
        settings = self.settings
        mapper = self.receipt_mapper
        if (
            not items
            or mapper is None
            or settings is None
            or not getattr(settings, "ai_configured", False)
        ):
            return

        async def _run() -> None:
            try:
                await mapper.map_unmapped_items(items, settings=settings)
            except Exception:
                _LOG.warning("Background receipt mapping failed", exc_info=True)

        def _thread() -> None:
            asyncio.run(_run())

        import threading

        threading.Thread(target=_thread, daemon=True).start()

    def register_adapter(
        self,
        provider: str,
        adapter: ProviderAdapter,
        *,
        account_id: str | None = None,
    ) -> None:
        """Register a provider adapter instance (optionally scoped to an account_id)."""
        p = provider.strip().lower()
        if p not in self.SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider: {provider}")
        if account_id is not None:
            cleaned = account_id.strip()
            if not cleaned:
                raise ValueError("account_id must be non-empty")
            self._account_adapters[(p, cleaned)] = adapter
        else:
            self._adapters[p] = adapter

    def unregister_adapter(self, provider: str, *, account_id: str | None = None) -> None:
        """Unregister a provider adapter instance."""
        p = provider.strip().lower()
        if account_id is not None:
            self._account_adapters.pop((p, account_id.strip()), None)
        else:
            self._adapters.pop(p, None)

    def get_adapter(self, provider: str, account_id: str) -> ProviderAdapter | None:
        """Retrieve registered adapter or construct configured service adapter."""
        p = provider.strip().lower()
        if (p, account_id) in self._account_adapters:
            return self._account_adapters[(p, account_id)]
        if (
            self.adapter_factory is not None
            and self.connector_store is not None
            and self.household_id is not None
            and p in ("rema1000", "coop", "trumf")
        ):
            creds = self.connector_store.get_credentials(
                self.household_id, p, account_id=account_id
            )
            if creds is not None:
                built = self.adapter_factory(p, account_id, creds)
                if built is not None:
                    return built
        if p in self._adapters:
            adapter = self._adapters[p]
            adapter_account = getattr(adapter, "account_id", None)
            if adapter_account is None or adapter_account == account_id:
                return adapter
        if p == "oda" and self.oda_service is not None:
            if getattr(self.oda_service, "configured", False):
                repo = getattr(self.oda_service, "repository", None)
                if repo is not None and repo.has_connection(account_id):
                    if self.oda_adapter_factory is not None:
                        return self.oda_adapter_factory(self.oda_service, account_id)
                    from home_kitchen.bootstrap import create_oda_purchase_adapter

                    return create_oda_purchase_adapter(self.oda_service, account_id)
        return None

    def _connected_account_ids(self, provider: str, default_account_id: str) -> list[str]:
        """Collect all known account IDs for a provider across store, adapters, and Oda."""
        p = provider.strip().lower()
        account_ids: list[str] = []

        def _add(acc: str | None) -> None:
            if acc and acc not in account_ids:
                account_ids.append(acc)

        for prov, acc in self._account_adapters:
            if prov == p:
                _add(acc)

        if (
            self.connector_store is not None
            and self.household_id is not None
            and p in ("rema1000", "coop", "trumf")
        ):
            for st in self.connector_store.list_credentials(self.household_id, p):
                _add(st.account_id)

        if (
            p == "oda"
            and self.oda_service is not None
            and getattr(self.oda_service, "configured", False)
        ):
            repo = getattr(self.oda_service, "repository", None)
            if repo is not None:
                list_actors = getattr(repo, "list_connected_actors", None)
                if callable(list_actors):
                    for actor in list_actors():
                        _add(actor)
                if repo.has_connection(default_account_id):
                    _add(default_account_id)

        if p in self._adapters:
            adapter_acc = getattr(self._adapters[p], "account_id", None)
            _add(adapter_acc or default_account_id)

        return account_ids

    def _resolve_accounts_for_provider(
        self, provider: str, default_account_id: str
    ) -> list[tuple[str, ProviderAdapter]]:
        p = provider.strip().lower()
        candidate_ids = self._connected_account_ids(p, default_account_id)
        if not candidate_ids:
            candidate_ids = [default_account_id]

        targets: list[tuple[str, ProviderAdapter]] = []
        for acc_id in candidate_ids:
            adapter = self.get_adapter(p, acc_id)
            if adapter is not None:
                targets.append((acc_id, adapter))

        # Preserve fallback for legacy adapters whose internal account_id was omitted
        if not targets and p in self._adapters:
            targets.append((default_account_id, self._adapters[p]))
        return targets

    def get_merged_records(
        self,
        provider: str | None = None,
        *,
        deduplicate_by_external_id: bool = False,
    ) -> list[dict[str, Any]]:
        """Return merged purchase records across all household accounts, deduplicated."""
        if self._order_store is None:
            return []

        rows = self._order_store.list_records(provider=provider)
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for row in rows:
            key = (
                (str(row["provider"]), str(row["external_id"]))
                if deduplicate_by_external_id
                else (
                    str(row["provider"]),
                    str(row["external_id"]),
                    str(row["revision_id"]),
                )
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "provider": str(row["provider"]),
                    "account_id": str(row["account_id"]),
                    "external_id": str(row["external_id"]),
                    "revision_id": str(row["revision_id"]),
                    "source_updated_at": _utc(row["source_updated_at"]),
                    "observed_at": _utc(row["observed_at"]),
                    "payload": row["payload"],
                }
            )
        return merged

    def get_effective_orders(
        self,
        provider: str | None = None,
        *,
        include_inactive: bool = True,
    ) -> list[EffectiveProviderOrder]:
        """Return resolved effective orders/receipts after applying corrections and returns."""
        if self._order_store is None:
            return []

        rows = self._order_store.list_effective_source_rows(provider=provider)
        effective = resolve_effective_orders(rows, provider=provider)
        if include_inactive:
            return effective
        return [order for order in effective if order.is_active_purchase]

    def get_bought_before_context(self, *, limit: int = 25) -> PurchaseHistoryContext:
        """Build bounded 'bought before' context from active resolved purchases only."""
        from home_kitchen.context import build_purchase_history_context

        effective = self.get_effective_orders(include_inactive=False)
        return build_purchase_history_context(effective, limit=limit)

    def _connected_store_auth_mode(
        self, provider: str, account_id: str | None = None
    ) -> str | None:
        if (
            self.connector_store is None
            or self.household_id is None
            or provider not in ("rema1000", "coop", "trumf")
        ):
            return None
        store_accounts = self.connector_store.list_credentials(self.household_id, provider)
        if not store_accounts:
            return None
        if account_id is not None:
            for acc in store_accounts:
                if acc.account_id == account_id and not acc.is_expired and acc.auth_mode:
                    return acc.auth_mode
            for acc in store_accounts:
                if acc.account_id == account_id and acc.auth_mode:
                    return acc.auth_mode
        active = [acc for acc in store_accounts if not acc.is_expired and acc.auth_mode]
        if active:
            return active[0].auth_mode
        return store_accounts[0].auth_mode or None

    def _default_provider_metadata(
        self, provider: str, account_id: str | None = None
    ) -> tuple[str, str | None, int | None]:
        store_auth_mode = self._connected_store_auth_mode(provider, account_id=account_id)
        if provider == "trumf":
            auth_mode = store_auth_mode or "manual_session_cookie"
            if store_auth_mode is not None:
                limitations = (
                    "Trumf retains at most the last 12 months of purchase history. "
                    "Authentication uses a browser session cookie."
                )
            else:
                limitations = (
                    "Trumf retains at most the last 12 months of purchase history. "
                    "Authentication requires a manual browser session cookie."
                )
            return (auth_mode, limitations, 12)
        if provider == "coop":
            return (
                store_auth_mode or "oidc_pkce",
                "Coop purchase history is derived from Android APK 4.17.3 OpenAPI reverse engineering. "
                "Continuation response schema is undocumented.",
                None,
            )
        if provider == "oda":
            return (
                "oauth_unattended",
                "Oda orders are provider evidence only; delivered orders do not confirm purchase or stock.",
                None,
            )
        if provider == "rema1000":
            auth_mode = store_auth_mode or "manual_session_token"
            if auth_mode == "oauth_pkce":
                limitations = (
                    "Rema 1000 retains at most the last 24 months of purchase history. "
                    "Authenticated via OAuth PKCE."
                )
            elif store_auth_mode is not None:
                limitations = (
                    "Rema 1000 retains at most the last 24 months of purchase history. "
                    "Authentication uses a session token."
                )
            else:
                limitations = (
                    "Rema 1000 retains at most the last 24 months of purchase history. "
                    "Authentication requires a manual session token."
                )
            return (auth_mode, limitations, 24)
        return ("unknown", None, None)

    def get_provider_status(self, provider: str, account_id: str = "default") -> ProviderStatusInfo:
        """Report honest connection, freshness, delays, and coverage for one provider."""
        p = provider.strip().lower()
        store_auth_mode = self._connected_store_auth_mode(p, account_id=account_id)
        auth_method, default_limitations, retained_months = self._default_provider_metadata(
            p, account_id=account_id
        )
        targets = self._resolve_accounts_for_provider(p, account_id)
        store_accounts = (
            self.connector_store.list_credentials(self.household_id, p)
            if self.connector_store is not None
            and self.household_id is not None
            and p in ("rema1000", "coop", "trumf")
            else []
        )

        adapter = targets[0][1] if targets else self.get_adapter(p, account_id)

        store_last_error = next(
            (
                str(acc.metadata["last_error"])
                for acc in store_accounts
                if isinstance(acc.metadata, dict)
                and isinstance(acc.metadata.get("last_error"), str)
                and acc.metadata["last_error"].strip()
            ),
            None,
        )

        # Determine connection & auth state
        if adapter is not None:
            status: Literal["connected", "not_configured", "unavailable", "degraded", "error"] = (
                "connected"
            )
            auth_state: Literal[
                "connected", "not_configured", "expired", "unavailable", "error"
            ] = "connected"
            limitations = default_limitations
            status_fn = getattr(adapter, "status", None)
            if callable(status_fn):
                adapter_stat = status_fn()
                if store_auth_mode is None and hasattr(adapter_stat, "limitations"):
                    limitations = adapter_stat.limitations
                if store_auth_mode is None and hasattr(adapter_stat, "auth_method"):
                    auth_method = str(adapter_stat.auth_method)
                if hasattr(adapter_stat, "history_window_months"):
                    retained_months = adapter_stat.history_window_months
            if store_accounts:
                active_store_accounts = [acc for acc in store_accounts if not acc.is_expired]
                if not active_store_accounts:
                    status = "unavailable"
                    auth_state = "expired"
                    auth_method = store_accounts[0].auth_mode
        elif store_accounts:
            active_store_accounts = [acc for acc in store_accounts if not acc.is_expired]
            if active_store_accounts:
                status = "connected"
                auth_state = "connected"
                auth_method = active_store_accounts[0].auth_mode
            else:
                status = "unavailable"
                auth_state = "expired"
                auth_method = store_accounts[0].auth_mode
            limitations = default_limitations
        elif p == "oda" and self.oda_service is not None and self.oda_service.configured:
            oda_stat = self.oda_service.status(account_id)
            if oda_stat.get("connected"):
                status = "connected"
                auth_state = "connected"
                limitations = default_limitations
            else:
                status = "unavailable"
                auth_state = "expired"
                limitations = f"Oda account is not connected: {oda_stat.get('detail', '')}"
        else:
            status = "not_configured"
            auth_state = "not_configured"
            limitations = (
                f"{p.capitalize()} integration is not configured or authenticated for this account."
            )

        last_auth_error = self._last_auth_error.get(p)
        last_error = self._last_sync_error.get(p) or last_auth_error or store_last_error
        if last_auth_error:
            auth_state = "expired"
            if status in ("connected", "degraded"):
                status = "degraded" if self._partial_auth_failure.get(p) else "unavailable"
        elif last_error and status == "connected":
            status = "degraded"

        # Query database metrics if engine is available
        last_sync: AwareDatetime | None = _utc(self._last_sync_time.get(p))
        total_records = 0
        posting_delay = PostingDelayInfo(
            tracked=False,
            latest_delay_seconds=None,
            description="No synced records observed yet.",
        )
        ingestion_delay = IngestionDelayInfo(
            tracked=False,
            latest_delay_seconds=None,
            target_latency_minutes=15,
            description="No synced records observed yet.",
        )
        coverage_window = CoverageWindow(
            start=None,
            end=None,
            total_records=0,
            retained_months=retained_months,
            description=f"{p.capitalize()} coverage window.",
        )

        known_ids = self._connected_account_ids(p, account_id)
        if known_ids:
            query_account_ids = list(dict.fromkeys([*known_ids, account_id]))
        else:
            query_account_ids = [account_id]

        if self._order_store is not None:
            try:
                cp_times, all_source_rows = self._order_store.get_provider_freshness_rows(
                    p, query_account_ids
                )
                for cp_time in cp_times:
                    if cp_time is not None:
                        norm_cp = _utc(cp_time)
                        if last_sync is None or (norm_cp and norm_cp > last_sync):
                            last_sync = norm_cp

                effective_orders = resolve_effective_orders(all_source_rows, provider=p)
                total_records = len(effective_orders)
                correction_count = sum(1 for o in effective_orders if o.is_correction)
                return_count = sum(1 for o in effective_orders if o.is_return)
                cancellation_count = sum(1 for o in effective_orders if o.is_cancellation)

                if total_records > 0:
                    recent_orders = effective_orders[:100]

                    tx_times = [
                        o.transaction_at for o in recent_orders if o.transaction_at is not None
                    ]

                    start_tx = min(tx_times) if tx_times else None
                    end_tx = max(tx_times) if tx_times else None
                    coverage_window = CoverageWindow(
                        start=_utc(start_tx),
                        end=_utc(end_tx),
                        total_records=total_records,
                        correction_count=correction_count,
                        return_count=return_count,
                        cancellation_count=cancellation_count,
                        retained_months=retained_months,
                        description=(
                            f"Historical coverage for {total_records} {p} order/receipt records."
                        ),
                    )

                    latest_rec = recent_orders[0]
                    latest_obs = _utc(latest_rec.observed_at)
                    latest_src = _utc(latest_rec.source_updated_at)
                    latest_tx = latest_rec.transaction_at

                    if latest_src is not None and latest_tx is not None:
                        p_delay = max(0.0, (latest_src - latest_tx).total_seconds())
                        posting_delay = PostingDelayInfo(
                            tracked=True,
                            latest_delay_seconds=round(p_delay, 1),
                            description=f"Provider posted record {p_delay / 60:.1f} minutes after purchase event.",
                        )
                    else:
                        posting_delay = PostingDelayInfo(
                            tracked=False,
                            latest_delay_seconds=None,
                            description=f"{p.capitalize()} does not report a separate source posting timestamp; posting delay is untracked by provider.",
                        )

                    if latest_src is not None and latest_obs is not None:
                        i_delay = max(0.0, (latest_obs - latest_src).total_seconds())
                        ingestion_delay = IngestionDelayInfo(
                            tracked=True,
                            latest_delay_seconds=round(i_delay, 1),
                            target_latency_minutes=15,
                            description=f"Kitchen ingested record {i_delay / 60:.1f} minutes after provider record availability.",
                        )
                    elif latest_tx is not None and latest_obs is not None:
                        i_delay = max(0.0, (latest_obs - latest_tx).total_seconds())
                        ingestion_delay = IngestionDelayInfo(
                            tracked=True,
                            latest_delay_seconds=round(i_delay, 1),
                            target_latency_minutes=15,
                            description=f"Kitchen ingested record {i_delay / 60:.1f} minutes after purchase event (provider lacks separate posting timestamp).",
                        )
                    else:
                        ingestion_delay = IngestionDelayInfo(
                            tracked=True,
                            latest_delay_seconds=None,
                            target_latency_minutes=15,
                            description="Ingestion latency tracked via periodic polling.",
                        )
            except Exception as exc:
                last_error = f"Database read failure: {exc}"
                status = "error"

        return ProviderStatusInfo(
            provider=p,
            status=status,
            auth_state=auth_state,
            auth_method=auth_method,
            last_successful_sync=last_sync,
            last_sync_error=last_error,
            coverage_window=coverage_window,
            provider_posting_delay=posting_delay,
            kitchen_ingestion_delay=ingestion_delay,
            limitations=limitations,
        )

    def get_status(self, account_id: str = "default") -> PurchaseHistoryStatusResponse:
        """Report unified status across all supported grocery providers."""
        providers_status = {
            p: self.get_provider_status(p, account_id=account_id) for p in self.SUPPORTED_PROVIDERS
        }
        if any(s.status == "error" for s in providers_status.values()):
            overall = "error"
        elif any(s.status == "degraded" for s in providers_status.values()):
            overall = "degraded"
        elif any(s.status == "connected" for s in providers_status.values()):
            overall = "connected"
        else:
            overall = "not_configured"

        return PurchaseHistoryStatusResponse(
            providers=providers_status,
            overall_status=overall,
            checked_at=datetime.now(UTC),
        )

    def sync_provider(
        self, provider: str, account_id: str = "default", max_pages: int = 50
    ) -> ProviderSyncResult:
        """Execute bounded sync pass across all connected accounts for one provider."""
        p = provider.strip().lower()
        if p not in self.SUPPORTED_PROVIDERS:
            return ProviderSyncResult(
                provider=p, status="failed", error=f"Unsupported provider: {provider}"
            )
        if self._order_store is None:
            return ProviderSyncResult(
                provider=p, status="skipped", error="Database engine unavailable"
            )

        targets = self._resolve_accounts_for_provider(p, account_id)
        if not targets:
            return ProviderSyncResult(
                provider=p, status="skipped", error=f"Provider {p} is not configured"
            )

        start_time = time.monotonic()
        total_records = 0
        accounts_synced = 0
        last_checkpoint: str | None = None
        errors: list[str] = []
        auth_errors: list[str] = []
        had_incomplete = False
        had_failure = False
        had_auth_failure = False

        order_store = self._order_store

        def _cached_kassal_factory(api_key: str) -> Any:
            factory = self.kassal_client_factory
            cached = self._kassal_clients_by_key.get(api_key)
            if cached is not None and cached[0] == factory:
                return cached[1]
            if factory is not None:
                client = factory(api_key)
            else:
                from home_kitchen.bootstrap import create_kassal_client

                client = create_kassal_client(api_key)
            self._kassal_clients_by_key[api_key] = (factory, client)
            return client

        def _run_sync(target_account_id: str, active_adapter: ProviderAdapter) -> int:
            return sync_account(
                order_store,
                p,
                target_account_id,
                active_adapter,
                kassal_client=self.kassal_client,
                kassal_client_factory=_cached_kassal_factory,
                identity_service=self.identity_service,
                settings=self.settings,
                connector_store=self.connector_store,
                household_id=self.household_id,
                on_unmapped_lines=self._dispatch_background_mapping,
            )

        for target_account_id, adapter in targets:
            active_adapter = adapter
            uses_managed_connector = (
                self.connector_auth is not None
                and self.connector_store is not None
                and self.household_id is not None
                and p in ("rema1000", "coop", "trumf")
                and (p, target_account_id) not in self._account_adapters
                and p not in self._adapters
            )
            if (
                uses_managed_connector
                and self.connector_store is not None
                and self.household_id is not None
            ):
                try:
                    meta = self.connector_store.get_connection_metadata(
                        self.household_id, p, account_id=target_account_id
                    )
                    if meta is not None:
                        self.connector_auth.ensure_fresh_credentials(
                            self.household_id,
                            None,
                            p,
                            account_id=target_account_id,
                        )
                        rebuilt = self.get_adapter(p, target_account_id)
                        if rebuilt is not None:
                            active_adapter = rebuilt
                except Exception as exc:
                    had_failure = True
                    last_checkpoint = order_store.get_checkpoint(p, target_account_id)
                    safe_msg = self._sanitize_account_error(p, target_account_id, exc)
                    errors.append(safe_msg)
                    if self._is_auth_exception(exc):
                        had_auth_failure = True
                        auth_errors.append(safe_msg)
                        self._record_account_auth_error(p, target_account_id, safe_msg)
                    continue

            try:
                count = _run_sync(target_account_id, active_adapter)
                total_records += count
                accounts_synced += 1
                last_checkpoint = order_store.get_checkpoint(p, target_account_id)
                self._clear_account_auth_error(p, target_account_id)
            except IncompleteScanError as exc:
                had_incomplete = True
                total_records += getattr(exc, "records_synced", 0)
                accounts_synced += 1
                last_checkpoint = order_store.get_checkpoint(p, target_account_id)
                errors.append(self._sanitize_account_error(p, target_account_id, exc))
            except Exception as exc:
                if (
                    self._is_auth_exception(exc)
                    and uses_managed_connector
                    and self.connector_auth is not None
                    and self.household_id is not None
                ):
                    try:
                        self.connector_auth.refresh_connection(
                            self.household_id,
                            None,
                            p,
                            account_id=target_account_id,
                        )
                        rebuilt = self.get_adapter(p, target_account_id)
                        if rebuilt is None:
                            raise exc
                        count = _run_sync(target_account_id, rebuilt)
                        total_records += count
                        accounts_synced += 1
                        last_checkpoint = order_store.get_checkpoint(p, target_account_id)
                        self._clear_account_auth_error(p, target_account_id)
                        continue
                    except IncompleteScanError as retry_exc:
                        had_incomplete = True
                        total_records += getattr(retry_exc, "records_synced", 0)
                        accounts_synced += 1
                        last_checkpoint = order_store.get_checkpoint(p, target_account_id)
                        errors.append(self._sanitize_account_error(p, target_account_id, retry_exc))
                        continue
                    except Exception as retry_exc:
                        exc = retry_exc

                had_failure = True
                last_checkpoint = order_store.get_checkpoint(p, target_account_id)
                safe_msg = self._sanitize_account_error(p, target_account_id, exc)
                errors.append(safe_msg)
                if self._is_auth_exception(exc):
                    had_auth_failure = True
                    auth_errors.append(safe_msg)
                    self._record_account_auth_error(p, target_account_id, safe_msg)

        duration = round(time.monotonic() - start_time, 3)
        error_text = "; ".join(errors) if errors else None
        auth_error_text = "; ".join(auth_errors) if auth_errors else None

        if not had_incomplete and not had_failure:
            self._last_sync_time[p] = datetime.now(UTC)
            self._last_sync_error[p] = None
            self._last_auth_error[p] = None
            self._partial_auth_failure[p] = False
            result_status: Literal["success", "skipped", "incomplete", "failed"] = "success"
        elif had_incomplete or (had_failure and accounts_synced > 0):
            if accounts_synced > 0:
                self._last_sync_time[p] = datetime.now(UTC)
            self._last_sync_error[p] = error_text
            self._last_auth_error[p] = auth_error_text
            self._partial_auth_failure[p] = had_auth_failure and accounts_synced > 0
            result_status = "incomplete"
        else:
            self._last_sync_error[p] = error_text
            self._last_auth_error[p] = auth_error_text
            self._partial_auth_failure[p] = False
            result_status = "failed"

        return ProviderSyncResult(
            provider=p,
            status=result_status,
            records_synced=total_records,
            accounts_synced=accounts_synced,
            checkpoint=last_checkpoint,
            duration_seconds=duration,
            error=error_text,
        )

    def sync(
        self,
        account_id: str = "default",
        provider: str | None = None,
        max_pages: int = 50,
    ) -> PurchaseHistorySyncResponse:
        """Execute bounded sync across all or selected providers."""
        target_providers = (
            [provider.strip().lower()] if provider else list(self.SUPPORTED_PROVIDERS)
        )
        results = {}
        for p in target_providers:
            results[p] = self.sync_provider(p, account_id=account_id, max_pages=max_pages)

        total_synced = sum(r.records_synced for r in results.values())
        return PurchaseHistorySyncResponse(
            results=results,
            total_records_synced=total_synced,
            completed_at=datetime.now(UTC),
        )

    async def sync_forever(
        self,
        interval_seconds: float = DEFAULT_PURCHASE_SYNC_INTERVAL_SECONDS,
        *,
        account_id: str = "default",
        max_pages: int = 50,
    ) -> None:
        """Periodically sync connected grocery purchase providers in the background."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        while True:
            try:
                task = asyncio.create_task(
                    asyncio.to_thread(self.sync, account_id=account_id, max_pages=max_pages)
                )
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    try:
                        await task
                    except Exception:
                        pass
                    raise
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.warning("Background purchase history sync failed; will retry on next run")
            await asyncio.sleep(interval_seconds)


def __getattr__(name: str) -> Any:
    if name in {
        "metadata",
        "provider_order_sources",
        "provider_order_checkpoints",
        "kassal_ean_cache",
        "checkpoint",
        "persist_page",
    }:
        from home_kitchen import purchase_sync_adapters

        return getattr(purchase_sync_adapters, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
