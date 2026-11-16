"""Encrypted, expiring OAuth records in the application's PostgreSQL database."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from cryptography.fernet import Fernet, InvalidToken
from key_value.aio._utils.managed_entry import ManagedEntry
from key_value.aio.stores.base import BaseStore
from sqlalchemy import Column, DateTime, MetaData, Table, Text, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool

oauth_records = Table(
    "kitchen_mcp_oauth",
    MetaData(),
    Column("collection", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("encrypted_payload", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True)),
)


class OAuthRegistrationLimitError(ValueError):
    """The bounded public client registry cannot accept this registration."""


class PostgresOAuthStore(BaseStore):
    """Share client registrations and token state across workers and restarts.

    The engine belongs to KitchenService and is disposed by the app lifespan.
    BaseStore supplies bulk operations and TTL semantics; database I/O runs off
    the event loop. Payloads bind their collection/key to prevent row swapping.
    """

    CLIENT_COLLECTION = "mcp-oauth-proxy-clients"
    PENDING_CLIENT_TTL = 24 * 3600
    MAX_CLIENTS = 1000
    MAX_CLIENT_RECORD_BYTES = 16 * 1024

    def __init__(self, engine: Engine, encryption_key: str):
        super().__init__(stable_api=True)
        self.engine = engine
        self.fernet = Fernet(encryption_key.encode())

    async def _get_managed_entry(self, *, collection: str, key: str) -> ManagedEntry | None:
        def read():
            with self.engine.begin() as connection:
                row = (
                    connection.execute(
                        select(oauth_records).where(
                            oauth_records.c.collection == collection,
                            oauth_records.c.key == sha256(key.encode()).hexdigest(),
                            or_(
                                oauth_records.c.expires_at.is_(None),
                                oauth_records.c.expires_at > func.now(),
                            ),
                        )
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                return None
            try:
                payload = json.loads(self.fernet.decrypt(row["encrypted_payload"].encode()))
                if payload["collection"] != collection or payload["key"] != key:
                    return None
                return ManagedEntry(
                    value=payload["value"],
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                )
            except (InvalidToken, ValueError, KeyError, TypeError):
                return None

        return await run_in_threadpool(read)

    async def _put_managed_entry(
        self, *, collection: str, key: str, managed_entry: ManagedEntry
    ) -> None:
        payload = json.dumps(
            {"collection": collection, "key": key, "value": dict(managed_entry.value)}
        ).encode()
        is_client = collection == self.CLIENT_COLLECTION
        if is_client and len(payload) > self.MAX_CLIENT_RECORD_BYTES:
            raise OAuthRegistrationLimitError("MCP client metadata is too large")
        expires_at = managed_entry.expires_at
        if is_client and expires_at is None:
            # Anonymous DCR requests cannot create permanent registry entries.
            expires_at = datetime.now(UTC) + timedelta(seconds=self.PENDING_CLIENT_TTL)
        values = {
            "collection": collection,
            # SDK keys include authorization codes: retain only their hash
            # outside the encrypted payload, never plaintext bearer material.
            "key": sha256(key.encode()).hexdigest(),
            "encrypted_payload": self.fernet.encrypt(payload).decode(),
            "created_at": managed_entry.created_at,
            "expires_at": expires_at,
        }

        def write():
            with self.engine.begin() as connection:
                if is_client:
                    # Serialize admission across all pods. Existing clients may
                    # renew at capacity; no live registration is evicted.
                    connection.execute(select(func.pg_advisory_xact_lock(0x4B49544348454E)))
                    existing = connection.execute(
                        select(oauth_records.c.key).where(
                            oauth_records.c.collection == collection,
                            oauth_records.c.key == values["key"],
                            or_(
                                oauth_records.c.expires_at.is_(None),
                                oauth_records.c.expires_at > func.now(),
                            ),
                        )
                    ).first()
                    count = connection.scalar(
                        select(func.count())
                        .select_from(oauth_records)
                        .where(
                            oauth_records.c.collection == collection,
                            or_(
                                oauth_records.c.expires_at.is_(None),
                                oauth_records.c.expires_at > func.now(),
                            ),
                        )
                    )
                    if existing is None and (count is None or count >= self.MAX_CLIENTS):
                        raise OAuthRegistrationLimitError(
                            "MCP client registry is full; try again after pending registrations expire"
                        )
                # Remove expired transactions during writes; registrations with
                # a live token lifetime remain available for refresh.
                connection.execute(
                    delete(oauth_records).where(oauth_records.c.expires_at <= func.now())
                )
                connection.execute(
                    insert(oauth_records)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[oauth_records.c.collection, oauth_records.c.key],
                        set_=values,
                    )
                )

        await run_in_threadpool(write)

    async def _delete_managed_entry(self, *, key: str, collection: str) -> bool:
        def remove():
            with self.engine.begin() as connection:
                return (
                    connection.execute(
                        delete(oauth_records)
                        .where(
                            oauth_records.c.collection == collection,
                            oauth_records.c.key == sha256(key.encode()).hexdigest(),
                        )
                        .returning(oauth_records.c.key)
                    ).first()
                    is not None
                )

        return await run_in_threadpool(remove)

    async def take(self, key: str, *, collection: str) -> dict | None:
        """Atomically consume a live code/transaction across all application pods."""

        def consume():
            with self.engine.begin() as connection:
                row = connection.execute(
                    delete(oauth_records)
                    .where(
                        oauth_records.c.collection == collection,
                        oauth_records.c.key == sha256(key.encode()).hexdigest(),
                        or_(
                            oauth_records.c.expires_at.is_(None),
                            oauth_records.c.expires_at > func.now(),
                        ),
                    )
                    .returning(oauth_records.c.encrypted_payload)
                ).first()
            if row is None:
                return None
            try:
                payload = json.loads(self.fernet.decrypt(row[0].encode()))
                if payload["collection"] != collection or payload["key"] != key:
                    return None
                return dict(payload["value"])
            except (InvalidToken, ValueError, KeyError, TypeError):
                return None

        return await run_in_threadpool(consume)
