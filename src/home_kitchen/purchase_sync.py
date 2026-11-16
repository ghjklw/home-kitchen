"""Provider-neutral capture of externally observed order records.

These captures never assert a purchase or connect to household history or inventory.
Adapters return ``has_more=True`` with a page-continuation cursor until backfill is
exhausted; the final page has ``has_more=False`` and carries the durable poll cursor
for the next sync. Revision IDs are immutable: corrections need a new revision ID.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
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
    def incomplete_scan_must_be_terminal(self) -> "ProviderPage":
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


def sync_account(engine: Engine, provider: str, account_id: str, adapter: ProviderAdapter) -> int:
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
        persist_page(engine, provider, account_id, page, expected_cursor=cursor)
        count += len(page.records)
        if page.incomplete_reason is not None:
            # Persist first: the terminal cursor allows the next poll to restart,
            # while the typed failure prevents claiming a completed backfill.
            raise IncompleteScanError(page.incomplete_reason)
        if not page.has_more:
            return count
        cursor = page.next_cursor
    raise ValueError("provider returned a repeated continuation cursor")
