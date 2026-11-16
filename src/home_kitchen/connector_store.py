"""Encrypted storage and status tracking for household external connectors."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    delete,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from home_kitchen.settings import Settings

metadata = MetaData()

connector_credentials = Table(
    "household_connector_credentials",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("household_id", Uuid, nullable=False, index=True),
    Column("provider_id", Text, nullable=False),
    Column("auth_mode", Text, nullable=False),
    Column("encrypted_credentials", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("actor_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("metadata_json", JSON().with_variant(JSONB, "postgresql"), nullable=True),
    UniqueConstraint("household_id", "provider_id", name="uq_household_connector_provider"),
)


def resolve_connector_cipher(settings: Settings) -> Fernet | None:
    """Resolve encryption cipher from connector or Oda key, or deterministic local_dev key."""
    key = settings.connector_encryption_key or settings.oda_encryption_key
    if key is not None:
        return Fernet(key.get_secret_value().encode("utf-8"))
    if settings.local_dev:
        deterministic_dev_key = base64.urlsafe_b64encode(
            hashlib.sha256(b"kitchen-local-dev-connector-key-seed").digest()
        )
        return Fernet(deterministic_dev_key)
    return None


def _parse_datetime(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=UTC)
        return val.astimezone(UTC)
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except (ValueError, TypeError):
            return None
    return None


def _parse_json(val: Any) -> dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _insert(table: Table, dialect_name: str):
    if dialect_name == "postgresql":
        return pg_insert(table)
    if dialect_name == "sqlite":
        return sqlite_insert(table)
    raise ValueError(f"unsupported database dialect: {dialect_name}")


class ConnectorStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    auth_mode: str
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    actor_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at < datetime.now(UTC)


class ConnectorStore:
    def __init__(self, engine: Engine, cipher: Fernet | None):
        self.engine = engine
        self.cipher = cipher

    def _require_cipher(self) -> Fernet:
        if self.cipher is None:
            raise RuntimeError(
                "Krypteringsnøkkel for tilkoblinger er ikke konfigurert "
                "(KITCHEN_CONNECTOR_ENCRYPTION_KEY eller KITCHEN_ODA_ENCRYPTION_KEY kreves)"
            )
        return self.cipher

    def encrypt(self, payload: dict[str, Any]) -> str:
        cipher = self._require_cipher()
        return cipher.encrypt(json.dumps(payload).encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_payload: str) -> dict[str, Any]:
        cipher = self._require_cipher()
        return json.loads(cipher.decrypt(encrypted_payload.encode("utf-8")).decode("utf-8"))

    def save_connection(
        self,
        household_id: UUID,
        provider_id: str,
        auth_mode: str,
        credentials: dict[str, Any],
        *,
        expires_at: datetime | None = None,
        actor_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConnectorStatus:
        now = datetime.now(UTC)
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            else:
                expires_at = expires_at.astimezone(UTC)

        encrypted = self.encrypt(credentials)
        dialect_name = self.engine.dialect.name
        stmt = _insert(connector_credentials, dialect_name)
        conflict_indices = [
            connector_credentials.c.household_id,
            connector_credentials.c.provider_id,
        ]
        values = {
            "id": uuid4(),
            "household_id": household_id,
            "provider_id": provider_id,
            "auth_mode": auth_mode,
            "encrypted_credentials": encrypted,
            "expires_at": expires_at,
            "actor_id": actor_id,
            "created_at": now,
            "updated_at": now,
            "metadata_json": metadata or {},
        }
        update_set = {
            "auth_mode": auth_mode,
            "encrypted_credentials": encrypted,
            "expires_at": expires_at,
            "actor_id": actor_id,
            "updated_at": now,
            "metadata_json": metadata or {},
        }

        query = stmt.values(**values).on_conflict_do_update(
            index_elements=conflict_indices,
            set_=update_set,
        )
        with self.engine.begin() as conn:
            conn.execute(query)

        return ConnectorStatus(
            provider_id=provider_id,
            auth_mode=auth_mode,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
            actor_id=actor_id,
            metadata=metadata or {},
        )

    def get_connection_metadata(
        self, household_id: UUID, provider_id: str
    ) -> ConnectorStatus | None:
        query = select(
            connector_credentials.c.provider_id,
            connector_credentials.c.auth_mode,
            connector_credentials.c.expires_at,
            connector_credentials.c.created_at,
            connector_credentials.c.updated_at,
            connector_credentials.c.actor_id,
            connector_credentials.c.metadata_json,
        ).where(
            connector_credentials.c.household_id == household_id,
            connector_credentials.c.provider_id == provider_id,
        )
        with self.engine.connect() as conn:
            row = conn.execute(query).one_or_none()
            if row is None:
                return None
            return ConnectorStatus(
                provider_id=str(row.provider_id),
                auth_mode=str(row.auth_mode),
                expires_at=_parse_datetime(row.expires_at),
                created_at=_parse_datetime(row.created_at) or datetime.now(UTC),
                updated_at=_parse_datetime(row.updated_at) or datetime.now(UTC),
                actor_id=str(row.actor_id) if row.actor_id is not None else None,
                metadata=_parse_json(row.metadata_json),
            )

    def get_credentials(self, household_id: UUID, provider_id: str) -> dict[str, Any] | None:
        query = select(connector_credentials.c.encrypted_credentials).where(
            connector_credentials.c.household_id == household_id,
            connector_credentials.c.provider_id == provider_id,
        )
        with self.engine.connect() as conn:
            encrypted = conn.execute(query).scalar_one_or_none()
            if encrypted is None:
                return None
            return self.decrypt(encrypted)

    def delete_connection(self, household_id: UUID, provider_id: str) -> bool:
        stmt = delete(connector_credentials).where(
            connector_credentials.c.household_id == household_id,
            connector_credentials.c.provider_id == provider_id,
        )
        with self.engine.begin() as conn:
            result = conn.execute(stmt)
            return bool((result.rowcount or 0) > 0)

    def list_connections(self, household_id: UUID) -> dict[str, ConnectorStatus]:
        query = select(
            connector_credentials.c.provider_id,
            connector_credentials.c.auth_mode,
            connector_credentials.c.expires_at,
            connector_credentials.c.created_at,
            connector_credentials.c.updated_at,
            connector_credentials.c.actor_id,
            connector_credentials.c.metadata_json,
        ).where(connector_credentials.c.household_id == household_id)
        results = {}
        with self.engine.connect() as conn:
            rows = conn.execute(query).all()
            for row in rows:
                provider_id = str(row.provider_id)
                results[provider_id] = ConnectorStatus(
                    provider_id=provider_id,
                    auth_mode=str(row.auth_mode),
                    expires_at=_parse_datetime(row.expires_at),
                    created_at=_parse_datetime(row.created_at) or datetime.now(UTC),
                    updated_at=_parse_datetime(row.updated_at) or datetime.now(UTC),
                    actor_id=str(row.actor_id) if row.actor_id is not None else None,
                    metadata=_parse_json(row.metadata_json),
                )
        return results
