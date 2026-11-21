"""Provider-neutral capture of externally observed order records.

These captures never assert a purchase or connect to household history or inventory.
Adapters return ``has_more=True`` with a page-continuation cursor until backfill is
exhausted; the final page has ``has_more=False`` and carries the durable poll cursor
for the next sync. Revision IDs are immutable: corrections need a new revision ID.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from home_kitchen.connector_store import ConnectorStore

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    and_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

_LOG = logging.getLogger(__name__)
DEFAULT_PURCHASE_SYNC_INTERVAL_SECONDS = 300.0
metadata = MetaData()
provider_order_sources = Table(
    "provider_order_sources",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("provider", String(100), nullable=False),
    Column("account_id", String(255), nullable=False),
    Column("external_id", String(512), nullable=False),
    Column("revision_id", String(512), nullable=False),
    Column("source_updated_at", DateTime(timezone=True), nullable=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON().with_variant(JSONB, "postgresql"), nullable=False),
    UniqueConstraint(
        "provider", "account_id", "external_id", "revision_id", name="uq_provider_order_revision"
    ),
)
provider_order_checkpoints = Table(
    "provider_order_checkpoints",
    metadata,
    Column("provider", String(100), primary_key=True),
    Column("account_id", String(255), primary_key=True),
    Column("cursor", String(2048), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


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


def _identity(provider: str, account_id: str) -> tuple[str, str]:
    provider, account_id = provider.strip(), account_id.strip()
    if not provider or not account_id:
        raise ValueError("provider and account_id must be non-empty")
    return provider, account_id


def _insert(table, dialect: str):
    if dialect == "postgresql":
        return pg_insert(table)
    if dialect == "sqlite":
        return sqlite_insert(table)
    raise ValueError(f"unsupported database dialect: {dialect}")


def checkpoint(engine: Engine, provider: str, account_id: str) -> str | None:
    provider, account_id = _identity(provider, account_id)
    with engine.connect() as connection:
        row = connection.execute(
            select(provider_order_checkpoints.c.cursor).where(
                provider_order_checkpoints.c.provider == provider,
                provider_order_checkpoints.c.account_id == account_id,
            )
        ).first()
    return row[0] if row else None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _persist_record(
    connection, provider: str, account_id: str, record, observed_at: datetime
) -> None:
    payload = record.model_dump(mode="json")["payload"]
    values = {
        "id": uuid4(),
        "provider": provider,
        "account_id": account_id,
        "external_id": record.external_id,
        "revision_id": record.revision_id,
        "source_updated_at": record.source_updated_at,
        "observed_at": observed_at,
        "payload": payload,
    }
    identity = [
        provider_order_sources.c.provider,
        provider_order_sources.c.account_id,
        provider_order_sources.c.external_id,
        provider_order_sources.c.revision_id,
    ]
    connection.execute(
        _insert(provider_order_sources, connection.dialect.name)
        .values(**values)
        .on_conflict_do_nothing(index_elements=identity)
    )
    existing = connection.execute(
        select(provider_order_sources.c.source_updated_at, provider_order_sources.c.payload).where(
            provider_order_sources.c.provider == provider,
            provider_order_sources.c.account_id == account_id,
            provider_order_sources.c.external_id == record.external_id,
            provider_order_sources.c.revision_id == record.revision_id,
        )
    ).one()
    if _utc(existing.source_updated_at) != record.source_updated_at or existing.payload != payload:
        raise RevisionConflict(
            "provider revision contents changed; corrections require a new revision_id"
        )


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
            digits = re.sub(r"\D", "", clean)
            if len(digits) >= 8:
                return digits
            if clean:
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
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def extract_line_unit(line: dict[str, Any]) -> str | None:
    for key in ("unit", "enhet", "enhetsType", "enhets_type", "unit_type", "unitOfMeasure"):
        val = line.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def enrich_and_map_receipt_record(
    record: ProviderOrderRecord,
    kassal_client: Any | None,
    identity_service: Any | None = None,
) -> list[dict[str, Any]]:
    """Enrich barcode lines via Kassal.app and resolve canonical product/unit identities.

    Returns unmapped line items that require async background AI mapping.
    """
    lines = extract_receipt_lines(record)
    if not lines:
        return []

    unmapped: list[dict[str, Any]] = []

    for line in lines:
        ean = extract_line_ean(line)
        kassal_prod = None
        if ean and kassal_client is not None:
            kassal_prod = kassal_client.lookup_ean(ean)
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


def persist_page(
    engine: Engine,
    provider: str,
    account_id: str,
    page: ProviderPage,
    *,
    expected_cursor: str | None,
) -> None:
    """Atomically persist source revisions and CAS the expected account cursor."""
    provider, account_id = _identity(provider, account_id)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        # Insert-if-absent plus row lock serializes first use and later updates. The
        # transaction rollback removes this provisional row if any source write fails.
        connection.execute(
            _insert(provider_order_checkpoints, connection.dialect.name)
            .values(
                provider=provider,
                account_id=account_id,
                cursor=expected_cursor or "",
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    provider_order_checkpoints.c.provider,
                    provider_order_checkpoints.c.account_id,
                ]
            )
        )
        cursor_row = connection.execute(
            select(provider_order_checkpoints.c.cursor)
            .where(
                provider_order_checkpoints.c.provider == provider,
                provider_order_checkpoints.c.account_id == account_id,
            )
            .with_for_update()
        ).one()
        if cursor_row.cursor != (expected_cursor or ""):
            raise CheckpointConflict(
                "account checkpoint advanced while provider page was in flight"
            )

        for record in page.records:
            _persist_record(connection, provider, account_id, record, now)

        result = connection.execute(
            update(provider_order_checkpoints)
            .where(
                and_(
                    provider_order_checkpoints.c.provider == provider,
                    provider_order_checkpoints.c.account_id == account_id,
                    provider_order_checkpoints.c.cursor == (expected_cursor or ""),
                )
            )
            .values(cursor=page.next_cursor, updated_at=now)
        )
        if result.rowcount != 1:
            raise CheckpointConflict("account checkpoint changed before page commit")


def sync_account(
    engine: Engine,
    provider: str,
    account_id: str,
    adapter: ProviderAdapter,
    *,
    kassal_client: Any | None = None,
    identity_service: Any | None = None,
    settings: Any | None = None,
    connector_store: ConnectorStore | None = None,
    household_id: UUID | None = None,
    on_unmapped_lines: Callable[[list[dict[str, Any]]], None] | None = None,
) -> int:
    """Fetch pages; terminal-page cursor is saved for the next provider poll."""
    provider, account_id = _identity(provider, account_id)
    cursor = checkpoint(engine, provider, account_id)
    seen: set[str | None] = set()
    count = 0
    while cursor not in seen:
        seen.add(cursor)
        page = adapter.fetch_page(account_id, cursor)
        if page.has_more and page.next_cursor == cursor:
            raise ValueError("provider returned a non-advancing continuation cursor")
        if page.has_more and page.next_cursor in seen:
            raise ValueError("provider returned a repeated continuation cursor")

        has_eans = any(
            any(extract_line_ean(line) for line in extract_receipt_lines(rec))
            for rec in page.records
        )
        if has_eans:
            active_kassal = kassal_client
            if active_kassal is None:
                if connector_store is not None and household_id is not None:
                    creds = connector_store.get_credentials(household_id, "kassal")
                    if creds and isinstance(creds, dict) and creds.get("api_key"):
                        from home_kitchen.kassal import KassalClient

                        active_kassal = KassalClient(creds["api_key"])
                if (
                    active_kassal is None
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
                        from home_kitchen.kassal import KassalClient

                        active_kassal = KassalClient(clean_k.strip())
            if active_kassal is None:
                from home_kitchen.kassal import KASSAL_MISSING_KEY_ERROR

                raise ValueError(KASSAL_MISSING_KEY_ERROR)
            kassal_client = active_kassal

        all_unmapped: list[dict[str, Any]] = []
        for rec in page.records:
            unmapped = enrich_and_map_receipt_record(rec, kassal_client, identity_service)
            if unmapped:
                all_unmapped.extend(unmapped)

        persist_page(engine, provider, account_id, page, expected_cursor=cursor)
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


class CoverageWindow(BaseModel):
    """Historical coverage and retention boundaries for a provider's records."""

    model_config = ConfigDict(extra="forbid")

    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    total_records: int = 0
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
        engine: Engine | None = None,
        settings: Any = None,
        oda_service: Any = None,
        adapters: dict[str, ProviderAdapter] | None = None,
        *,
        connector_store: ConnectorStore | None = None,
        household_id: UUID | None = None,
        adapter_factory: Callable[[str, str, dict[str, Any]], ProviderAdapter | None] | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.oda_service = oda_service
        self.connector_store = connector_store
        self.household_id = household_id
        self.adapter_factory = adapter_factory
        self._adapters: dict[str, ProviderAdapter] = dict(adapters or {})
        self._account_adapters: dict[tuple[str, str], ProviderAdapter] = {}
        self._last_sync_time: dict[str, datetime] = {}
        self._last_sync_error: dict[str, str | None] = {}
        self.identity_service: Any = None
        self.mapping_store: Any = None
        self.kassal_client: Any = None

    def _dispatch_background_mapping(self, items: list[dict[str, Any]]) -> None:
        """Run AI/variant mapping for unmapped receipt lines without blocking sync."""
        settings = self.settings
        if not items or settings is None or not getattr(settings, "ai_configured", False):
            return
        identity_service = getattr(self, "identity_service", None)
        store = getattr(self, "mapping_store", None)

        async def _run() -> None:
            try:
                from home_kitchen.catalogue.ai_mapper import auto_map_unmapped_receipt_items_async

                await auto_map_unmapped_receipt_items_async(
                    store, settings, items, identity_service=identity_service
                )
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
                    from home_kitchen.oda.purchase_history import OdaPurchaseHistoryAdapter

                    return OdaPurchaseHistoryAdapter(self.oda_service, account_id)
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
        if self.engine is None:
            return []

        query = select(
            provider_order_sources.c.provider,
            provider_order_sources.c.account_id,
            provider_order_sources.c.external_id,
            provider_order_sources.c.revision_id,
            provider_order_sources.c.source_updated_at,
            provider_order_sources.c.observed_at,
            provider_order_sources.c.payload,
        )
        if provider is not None:
            query = query.where(provider_order_sources.c.provider == provider.strip().lower())
        query = query.order_by(
            provider_order_sources.c.observed_at.desc(),
            provider_order_sources.c.source_updated_at.desc(),
        )

        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        with self.engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
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
            if auth_mode == "oauth_pkce":
                limitations = (
                    "Trumf retains at most the last 12 months of purchase history. "
                    "Authenticated via OAuth PKCE."
                )
            elif auth_mode == "interactive_otp":
                limitations = (
                    "Trumf retains at most the last 12 months of purchase history. "
                    "Authenticated via interactive OTP."
                )
            elif store_auth_mode is not None:
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

        last_error = self._last_sync_error.get(p)
        if last_error:
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

        if self.engine is not None:
            try:
                with self.engine.connect() as connection:
                    cp_rows = connection.execute(
                        select(provider_order_checkpoints.c.updated_at).where(
                            provider_order_checkpoints.c.provider == p,
                            provider_order_checkpoints.c.account_id.in_(query_account_ids),
                        )
                    ).all()
                    for cp_row in cp_rows:
                        if cp_row[0]:
                            cp_time = _utc(cp_row[0])
                            if last_sync is None or (cp_time and cp_time > last_sync):
                                last_sync = cp_time

                    all_source_rows = (
                        connection.execute(
                            select(
                                provider_order_sources.c.external_id,
                                provider_order_sources.c.revision_id,
                                provider_order_sources.c.source_updated_at,
                                provider_order_sources.c.observed_at,
                                provider_order_sources.c.payload,
                            )
                            .where(
                                provider_order_sources.c.provider == p,
                                provider_order_sources.c.account_id.in_(query_account_ids),
                            )
                            .order_by(provider_order_sources.c.observed_at.desc())
                        )
                        .mappings()
                        .all()
                    )

                    deduped_records = []
                    seen_revisions: set[tuple[str, str]] = set()
                    for r in all_source_rows:
                        rev_key = (str(r["external_id"]), str(r["revision_id"]))
                        if rev_key in seen_revisions:
                            continue
                        seen_revisions.add(rev_key)
                        deduped_records.append(r)

                    total_records = len(deduped_records)

                    if total_records > 0:
                        records = deduped_records[:100]

                        tx_times = []
                        for r in records:
                            tx = extract_transaction_time(p, r["payload"])
                            if tx is not None:
                                tx_times.append(tx)

                        start_tx = min(tx_times) if tx_times else None
                        end_tx = max(tx_times) if tx_times else None
                        coverage_window = CoverageWindow(
                            start=_utc(start_tx),
                            end=_utc(end_tx),
                            total_records=total_records,
                            retained_months=retained_months,
                            description=(
                                f"Historical coverage for {total_records} {p} order/receipt records."
                            ),
                        )

                        latest_rec = records[0]
                        latest_obs = _utc(latest_rec["observed_at"])
                        latest_src = _utc(latest_rec["source_updated_at"])
                        latest_tx = extract_transaction_time(p, latest_rec["payload"])

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
        if self.engine is None:
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
        had_incomplete = False
        had_failure = False

        for target_account_id, adapter in targets:
            try:
                count = sync_account(
                    self.engine,
                    p,
                    target_account_id,
                    adapter,
                    kassal_client=getattr(self, "kassal_client", None),
                    identity_service=getattr(self, "identity_service", None),
                    settings=self.settings,
                    connector_store=self.connector_store,
                    household_id=self.household_id,
                    on_unmapped_lines=self._dispatch_background_mapping,
                )
                total_records += count
                accounts_synced += 1
                last_checkpoint = checkpoint(self.engine, p, target_account_id)
            except IncompleteScanError as exc:
                had_incomplete = True
                total_records += getattr(exc, "records_synced", 0)
                accounts_synced += 1
                last_checkpoint = checkpoint(self.engine, p, target_account_id)
                errors.append(str(exc))
            except Exception as exc:
                had_failure = True
                last_checkpoint = checkpoint(self.engine, p, target_account_id)
                errors.append(str(exc))

        duration = round(time.monotonic() - start_time, 3)
        error_text = "; ".join(errors) if errors else None

        if not had_incomplete and not had_failure:
            self._last_sync_time[p] = datetime.now(UTC)
            self._last_sync_error[p] = None
            result_status: Literal["success", "skipped", "incomplete", "failed"] = "success"
        elif had_incomplete or (had_failure and accounts_synced > 0):
            if accounts_synced > 0:
                self._last_sync_time[p] = datetime.now(UTC)
            self._last_sync_error[p] = error_text
            result_status = "incomplete"
        else:
            self._last_sync_error[p] = error_text
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
