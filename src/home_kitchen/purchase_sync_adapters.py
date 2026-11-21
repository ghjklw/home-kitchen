"""PostgreSQL / SQLAlchemy and in-memory adapters for provider purchase order staging."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

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
    inspect,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from home_kitchen.purchase_sync import (
    CheckpointConflict,
    ProviderPage,
    RevisionConflict,
    _identity,
    _utc,
)

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


def _insert(table: Table, dialect: str):
    if dialect == "postgresql":
        return pg_insert(table)
    if dialect == "sqlite":
        return sqlite_insert(table)
    raise ValueError(f"unsupported database dialect: {dialect}")


def checkpoint(engine: Engine, provider: str, account_id: str) -> str | None:
    """Return the durable continuation/poll cursor for one provider account."""
    provider, account_id = _identity(provider, account_id)
    with engine.connect() as connection:
        row = connection.execute(
            select(provider_order_checkpoints.c.cursor).where(
                provider_order_checkpoints.c.provider == provider,
                provider_order_checkpoints.c.account_id == account_id,
            )
        ).first()
    return row[0] if row else None


def _persist_record(
    connection: Connection, provider: str, account_id: str, record: Any, observed_at: datetime
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

        for idx, record in enumerate(page.records):
            _persist_record(
                connection,
                provider,
                account_id,
                record,
                now + timedelta(microseconds=idx),
            )

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


class PostgresPurchaseOrderStore:
    """SQLAlchemy secondary adapter implementing `PurchaseOrderStore`."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get_checkpoint(self, provider: str, account_id: str) -> str | None:
        return checkpoint(self.engine, provider, account_id)

    def persist_page(
        self,
        provider: str,
        account_id: str,
        page: ProviderPage,
        *,
        expected_cursor: str | None,
    ) -> None:
        persist_page(self.engine, provider, account_id, page, expected_cursor=expected_cursor)

    def list_records(self, provider: str | None = None) -> list[dict[str, Any]]:
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
        with self.engine.connect() as connection:
            if not inspect(connection).has_table("provider_order_sources"):
                return []
            rows = connection.execute(query).mappings().all()
            return [
                {
                    "provider": str(row["provider"]),
                    "account_id": str(row["account_id"]),
                    "external_id": str(row["external_id"]),
                    "revision_id": str(row["revision_id"]),
                    "source_updated_at": _utc(row["source_updated_at"]),
                    "observed_at": _utc(row["observed_at"]),
                    "payload": row["payload"],
                }
                for row in rows
            ]

    def list_effective_source_rows(
        self, provider: str | None = None
    ) -> Sequence[Mapping[str, Any]]:
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
        with self.engine.connect() as connection:
            if not inspect(connection).has_table("provider_order_sources"):
                return []
            return [dict(row) for row in connection.execute(query).mappings().all()]

    def get_provider_freshness_rows(
        self, provider: str, account_ids: Sequence[str]
    ) -> tuple[list[datetime | None], Sequence[Mapping[str, Any]]]:
        p = provider.strip().lower()
        with self.engine.connect() as connection:
            cp_rows = connection.execute(
                select(provider_order_checkpoints.c.updated_at).where(
                    provider_order_checkpoints.c.provider == p,
                    provider_order_checkpoints.c.account_id.in_(account_ids),
                )
            ).all()
            source_rows = (
                connection.execute(
                    select(
                        provider_order_sources.c.provider,
                        provider_order_sources.c.account_id,
                        provider_order_sources.c.external_id,
                        provider_order_sources.c.revision_id,
                        provider_order_sources.c.source_updated_at,
                        provider_order_sources.c.observed_at,
                        provider_order_sources.c.payload,
                    )
                    .where(
                        provider_order_sources.c.provider == p,
                        provider_order_sources.c.account_id.in_(account_ids),
                    )
                    .order_by(provider_order_sources.c.observed_at.desc())
                )
                .mappings()
                .all()
            )
            return [_utc(row[0]) for row in cp_rows], [dict(row) for row in source_rows]


class MemoryPurchaseOrderStore:
    """In-memory secondary adapter implementing `PurchaseOrderStore` for unit tests."""

    def __init__(self) -> None:
        self._checkpoints: dict[tuple[str, str], tuple[str, datetime]] = {}
        self._sources: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def get_checkpoint(self, provider: str, account_id: str) -> str | None:
        provider, account_id = _identity(provider, account_id)
        entry = self._checkpoints.get((provider, account_id))
        return entry[0] if entry is not None else None

    def persist_page(
        self,
        provider: str,
        account_id: str,
        page: ProviderPage,
        *,
        expected_cursor: str | None,
    ) -> None:
        provider, account_id = _identity(provider, account_id)
        now = datetime.now(UTC)
        cp_snapshot = dict(self._checkpoints)
        src_snapshot = dict(self._sources)
        try:
            current = self._checkpoints.get((provider, account_id), ("", now))[0]
            if current != (expected_cursor or ""):
                raise CheckpointConflict(
                    "account checkpoint advanced while provider page was in flight"
                )
            for idx, record in enumerate(page.records):
                key = (provider, account_id, record.external_id, record.revision_id)
                payload = record.model_dump(mode="json")["payload"]
                existing = self._sources.get(key)
                if existing is not None:
                    if (
                        _utc(existing["source_updated_at"]) != record.source_updated_at
                        or existing["payload"] != payload
                    ):
                        raise RevisionConflict(
                            "provider revision contents changed; corrections require a new revision_id"
                        )
                else:
                    self._sources[key] = {
                        "id": uuid4(),
                        "provider": provider,
                        "account_id": account_id,
                        "external_id": record.external_id,
                        "revision_id": record.revision_id,
                        "source_updated_at": record.source_updated_at,
                        "observed_at": now + timedelta(microseconds=idx),
                        "payload": payload,
                    }
            self._checkpoints[(provider, account_id)] = (page.next_cursor, now)
        except Exception:
            self._checkpoints = cp_snapshot
            self._sources = src_snapshot
            raise

    def list_records(self, provider: str | None = None) -> list[dict[str, Any]]:
        rows = list(self._sources.values())
        if provider is not None:
            p = provider.strip().lower()
            rows = [r for r in rows if r["provider"] == p]
        rows.sort(
            key=lambda r: (
                r["observed_at"],
                r["source_updated_at"] or datetime.min.replace(tzinfo=UTC),
            ),
            reverse=True,
        )
        return [
            {
                "provider": r["provider"],
                "account_id": r["account_id"],
                "external_id": r["external_id"],
                "revision_id": r["revision_id"],
                "source_updated_at": _utc(r["source_updated_at"]),
                "observed_at": _utc(r["observed_at"]),
                "payload": r["payload"],
            }
            for r in rows
        ]

    def list_effective_source_rows(
        self, provider: str | None = None
    ) -> Sequence[Mapping[str, Any]]:
        return self.list_records(provider=provider)

    def get_provider_freshness_rows(
        self, provider: str, account_ids: Sequence[str]
    ) -> tuple[list[datetime | None], Sequence[Mapping[str, Any]]]:
        p = provider.strip().lower()
        account_set = set(account_ids)
        cp_times = [
            _utc(updated_at)
            for (prov, acc), (_, updated_at) in self._checkpoints.items()
            if prov == p and acc in account_set
        ]
        source_rows = [
            dict(r)
            for r in self._sources.values()
            if r["provider"] == p and r["account_id"] in account_set
        ]
        source_rows.sort(key=lambda r: r["observed_at"], reverse=True)
        return cp_times, source_rows
