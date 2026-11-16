"""Provider-neutral storage for externally observed order records.

These rows are source captures only. This module deliberately does not interpret
an order as a purchase or connect provider data to household history or inventory.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    select,
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
    Column("source_updated_at", DateTime(timezone=True), nullable=False),
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
    Column("cursor", String(2048)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class ProviderOrderRecord(BaseModel):
    """One immutable source revision; payload meaning is intentionally opaque."""

    model_config = ConfigDict(extra="forbid")

    external_id: str
    revision_id: str
    source_updated_at: datetime
    payload: dict[str, object]

    @field_validator("external_id", "revision_id")
    @classmethod
    def identity_must_be_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("external identity fields must be non-empty")
        return value

    @field_validator("source_updated_at")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source_updated_at must include a timezone")
        return value.astimezone(UTC)


class ProviderPage(BaseModel):
    records: Sequence[ProviderOrderRecord]
    next_cursor: str | None


class ProviderAdapter(Protocol):
    def fetch_page(self, account_id: str, cursor: str | None) -> ProviderPage: ...


def _identity(provider: str, account_id: str) -> tuple[str, str]:
    provider, account_id = provider.strip(), account_id.strip()
    if not provider or not account_id:
        raise ValueError("provider and account_id must be non-empty")
    return provider, account_id


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


def persist_page(
    engine: Engine, provider: str, account_id: str, page: ProviderPage
) -> None:
    """Atomically persist source revisions and the cursor for the next page."""
    provider, account_id = _identity(provider, account_id)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for record in page.records:
            values = {
                "id": uuid4(),
                "provider": provider,
                "account_id": account_id,
                "external_id": record.external_id,
                "revision_id": record.revision_id,
                "source_updated_at": record.source_updated_at,
                "payload": record.payload,
            }
            statement = (
                pg_insert(provider_order_sources)
                if connection.dialect.name == "postgresql"
                else sqlite_insert(provider_order_sources)
            )
            connection.execute(
                statement.values(**values)
                .on_conflict_do_update(
                    index_elements=[
                        provider_order_sources.c.provider,
                        provider_order_sources.c.account_id,
                        provider_order_sources.c.external_id,
                        provider_order_sources.c.revision_id,
                    ],
                    set_={
                        "source_updated_at": record.source_updated_at,
                        "payload": record.payload,
                    },
                )
            )
        checkpoint_values = {
            "provider": provider,
            "account_id": account_id,
            "cursor": page.next_cursor,
            "updated_at": now,
        }
        statement = (
            pg_insert(provider_order_checkpoints)
            if connection.dialect.name == "postgresql"
            else sqlite_insert(provider_order_checkpoints)
        )
        connection.execute(
            statement.values(**checkpoint_values)
            .on_conflict_do_update(
                index_elements=[
                    provider_order_checkpoints.c.provider,
                    provider_order_checkpoints.c.account_id,
                ],
                set_={"cursor": page.next_cursor, "updated_at": now},
            )
        )


def sync_account(engine: Engine, provider: str, account_id: str, adapter: ProviderAdapter) -> int:
    """Page an adapter from its durable cursor; return count of captured revisions."""
    provider, account_id = _identity(provider, account_id)
    cursor = checkpoint(engine, provider, account_id)
    seen: set[str | None] = set()
    count = 0
    while cursor not in seen:
        seen.add(cursor)
        page = adapter.fetch_page(account_id, cursor)
        if page.next_cursor is not None and page.next_cursor == cursor:
            raise ValueError("provider returned a non-advancing cursor")
        persist_page(engine, provider, account_id, page)
        count += len(page.records)
        if page.next_cursor is None:
            return count
        cursor = page.next_cursor
    raise ValueError("provider returned a repeated cursor")
