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
    case,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from home_kitchen.settings import Settings

DEFAULT_ACCOUNT_ID = "default"

metadata = MetaData()

connector_credentials = Table(
    "household_connector_credentials",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("household_id", Uuid, nullable=False, index=True),
    Column("provider_id", Text, nullable=False),
    Column("account_id", Text, nullable=False, server_default=DEFAULT_ACCOUNT_ID),
    Column("account_label", Text, nullable=True),
    Column("auth_mode", Text, nullable=False),
    Column("encrypted_credentials", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("actor_id", Text, nullable=True),
    Column("connected_by_actor_id", Text, nullable=True),
    Column("updated_by_actor_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("metadata_json", JSON().with_variant(JSONB, "postgresql"), nullable=True),
    UniqueConstraint(
        "household_id",
        "provider_id",
        "account_id",
        name="uq_household_connector_provider_account",
    ),
)


def generate_account_id() -> str:
    """Generate a short unique account identifier for a new provider account."""
    return f"acc_{uuid4().hex[:8]}"


def _normalize_account_id(account_id: str | None) -> str | None:
    if account_id is None:
        return None
    cleaned = account_id.strip()
    return cleaned or None


def _normalize_account_label(account_label: str | None) -> str | None:
    if account_label is None:
        return None
    cleaned = account_label.strip()
    return cleaned or None


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


def _row_to_status(row: Any) -> ConnectorStatus:
    return ConnectorStatus(
        provider_id=str(row.provider_id),
        account_id=str(row.account_id),
        account_label=str(row.account_label) if row.account_label is not None else None,
        auth_mode=str(row.auth_mode),
        expires_at=_parse_datetime(row.expires_at),
        created_at=_parse_datetime(row.created_at) or datetime.now(UTC),
        updated_at=_parse_datetime(row.updated_at) or datetime.now(UTC),
        actor_id=str(row.actor_id) if row.actor_id is not None else None,
        connected_by_actor_id=(
            str(row.connected_by_actor_id) if row.connected_by_actor_id is not None else None
        ),
        updated_by_actor_id=(
            str(row.updated_by_actor_id) if row.updated_by_actor_id is not None else None
        ),
        metadata=_parse_json(row.metadata_json),
    )


class ConnectorStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    account_id: str = DEFAULT_ACCOUNT_ID
    account_label: str | None = None
    auth_mode: str
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    actor_id: str | None = None
    connected_by_actor_id: str | None = None
    updated_by_actor_id: str | None = None
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

    def resolve_account_id(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str | None = None,
        *,
        account_label: str | None = None,
        add_account: bool = False,
    ) -> str:
        """Resolve which account_id a write targets without overwriting other accounts."""
        cleaned_id = _normalize_account_id(account_id)
        if add_account or (cleaned_id is not None and cleaned_id.lower() == "new"):
            return generate_account_id()
        if cleaned_id is not None:
            return cleaned_id

        existing = self.list_credentials(household_id, provider_id)
        if not existing:
            return DEFAULT_ACCOUNT_ID

        cleaned_label = _normalize_account_label(account_label)
        if cleaned_label is not None:
            for item in existing:
                if item.account_label == cleaned_label:
                    return item.account_id
            return generate_account_id()

        for item in existing:
            if item.account_id == DEFAULT_ACCOUNT_ID:
                return DEFAULT_ACCOUNT_ID
        return existing[0].account_id

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
        account_id: str | None = None,
        account_label: str | None = None,
        add_account: bool = False,
    ) -> ConnectorStatus:
        now = datetime.now(UTC)
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            else:
                expires_at = expires_at.astimezone(UTC)

        resolved_account_id = self.resolve_account_id(
            household_id,
            provider_id,
            account_id=account_id,
            account_label=account_label,
            add_account=add_account,
        )
        normalized_label = _normalize_account_label(account_label)

        encrypted = self.encrypt(credentials)
        dialect_name = self.engine.dialect.name
        stmt = _insert(connector_credentials, dialect_name)
        conflict_indices = [
            connector_credentials.c.household_id,
            connector_credentials.c.provider_id,
            connector_credentials.c.account_id,
        ]
        values = {
            "id": uuid4(),
            "household_id": household_id,
            "provider_id": provider_id,
            "account_id": resolved_account_id,
            "account_label": normalized_label,
            "auth_mode": auth_mode,
            "encrypted_credentials": encrypted,
            "expires_at": expires_at,
            "actor_id": actor_id,
            "connected_by_actor_id": actor_id,
            "updated_by_actor_id": actor_id,
            "created_at": now,
            "updated_at": now,
            "metadata_json": metadata or {},
        }
        update_set: dict[str, Any] = {
            "auth_mode": auth_mode,
            "encrypted_credentials": encrypted,
            "expires_at": expires_at,
            "actor_id": (actor_id if actor_id is not None else connector_credentials.c.actor_id),
            "connected_by_actor_id": func.coalesce(
                connector_credentials.c.connected_by_actor_id,
                stmt.excluded.connected_by_actor_id,
            ),
            "updated_by_actor_id": (
                actor_id if actor_id is not None else connector_credentials.c.updated_by_actor_id
            ),
            "updated_at": now,
            "metadata_json": metadata or {},
        }
        if account_label is not None:
            update_set["account_label"] = normalized_label

        query = stmt.values(**values).on_conflict_do_update(
            index_elements=conflict_indices,
            set_=update_set,
        )
        with self.engine.begin() as conn:
            conn.execute(query)

        saved = self.get_connection_metadata(
            household_id, provider_id, account_id=resolved_account_id
        )
        if saved is not None:
            return saved
        return ConnectorStatus(
            provider_id=provider_id,
            account_id=resolved_account_id,
            account_label=normalized_label,
            auth_mode=auth_mode,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
            actor_id=actor_id,
            connected_by_actor_id=actor_id,
            updated_by_actor_id=actor_id,
            metadata=metadata or {},
        )

    def upsert_account_credentials(
        self,
        household_id: UUID,
        provider_id: str,
        auth_mode: str,
        credentials: dict[str, Any],
        *,
        account_id: str | None = None,
        account_label: str | None = None,
        expires_at: datetime | None = None,
        actor_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        add_account: bool = False,
    ) -> ConnectorStatus:
        return self.save_connection(
            household_id=household_id,
            provider_id=provider_id,
            auth_mode=auth_mode,
            credentials=credentials,
            expires_at=expires_at,
            actor_id=actor_id,
            metadata=metadata,
            account_id=account_id,
            account_label=account_label,
            add_account=add_account,
        )

    upsert_credentials = upsert_account_credentials

    def update_account_metadata(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str,
        metadata_updates: dict[str, Any],
        *,
        remove_keys: tuple[str, ...] = (),
    ) -> None:
        cleaned_account_id = _normalize_account_id(account_id)
        if not cleaned_account_id:
            return
        with self.engine.begin() as conn:
            existing_json = conn.execute(
                select(connector_credentials.c.metadata_json).where(
                    connector_credentials.c.household_id == household_id,
                    connector_credentials.c.provider_id == provider_id,
                    connector_credentials.c.account_id == cleaned_account_id,
                )
            ).scalar_one_or_none()
            if existing_json is None:
                return
            merged = dict(_parse_json(existing_json))
            for key in remove_keys:
                merged.pop(key, None)
            merged.update(metadata_updates)
            conn.execute(
                update(connector_credentials)
                .where(
                    connector_credentials.c.household_id == household_id,
                    connector_credentials.c.provider_id == provider_id,
                    connector_credentials.c.account_id == cleaned_account_id,
                )
                .values(metadata_json=merged, updated_at=datetime.now(UTC))
            )

    def _status_columns(self):
        return (
            connector_credentials.c.provider_id,
            connector_credentials.c.account_id,
            connector_credentials.c.account_label,
            connector_credentials.c.auth_mode,
            connector_credentials.c.expires_at,
            connector_credentials.c.created_at,
            connector_credentials.c.updated_at,
            connector_credentials.c.actor_id,
            connector_credentials.c.connected_by_actor_id,
            connector_credentials.c.updated_by_actor_id,
            connector_credentials.c.metadata_json,
        )

    def get_connection_metadata(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str | None = None,
    ) -> ConnectorStatus | None:
        cleaned_id = _normalize_account_id(account_id)
        query = select(*self._status_columns()).where(
            connector_credentials.c.household_id == household_id,
            connector_credentials.c.provider_id == provider_id,
        )
        if cleaned_id is not None:
            query = query.where(connector_credentials.c.account_id == cleaned_id)
        else:
            query = query.order_by(
                case((connector_credentials.c.account_id == DEFAULT_ACCOUNT_ID, 0), else_=1),
                connector_credentials.c.created_at.asc(),
                connector_credentials.c.account_id.asc(),
            )

        with self.engine.connect() as conn:
            row = conn.execute(query).first()
            if row is None:
                return None
            return _row_to_status(row)

    def get_credentials(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str | None = None,
    ) -> dict[str, Any] | None:
        cleaned_id = _normalize_account_id(account_id)
        query = select(connector_credentials.c.encrypted_credentials).where(
            connector_credentials.c.household_id == household_id,
            connector_credentials.c.provider_id == provider_id,
        )
        if cleaned_id is not None:
            query = query.where(connector_credentials.c.account_id == cleaned_id)
        else:
            query = query.order_by(
                case((connector_credentials.c.account_id == DEFAULT_ACCOUNT_ID, 0), else_=1),
                connector_credentials.c.created_at.asc(),
                connector_credentials.c.account_id.asc(),
            )
        with self.engine.connect() as conn:
            encrypted = conn.execute(query).scalar()
            if encrypted is None:
                return None
            return self.decrypt(encrypted)

    def delete_connection(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str | None = None,
    ) -> bool:
        cleaned_id = _normalize_account_id(account_id)
        stmt = delete(connector_credentials).where(
            connector_credentials.c.household_id == household_id,
            connector_credentials.c.provider_id == provider_id,
        )
        if cleaned_id is not None:
            stmt = stmt.where(connector_credentials.c.account_id == cleaned_id)
        with self.engine.begin() as conn:
            result = conn.execute(stmt)
            return bool((result.rowcount or 0) > 0)

    delete_credentials = delete_connection

    def delete_account_credentials(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str,
    ) -> bool:
        cleaned_id = _normalize_account_id(account_id)
        if cleaned_id is None:
            raise ValueError("account_id must be non-empty")
        return self.delete_connection(household_id, provider_id, account_id=cleaned_id)

    def list_credentials(
        self,
        household_id: UUID,
        provider_id: str | None = None,
    ) -> list[ConnectorStatus]:
        query = select(*self._status_columns()).where(
            connector_credentials.c.household_id == household_id
        )
        if provider_id is not None:
            query = query.where(connector_credentials.c.provider_id == provider_id)
        query = query.order_by(
            connector_credentials.c.provider_id.asc(),
            case((connector_credentials.c.account_id == DEFAULT_ACCOUNT_ID, 0), else_=1),
            connector_credentials.c.created_at.asc(),
            connector_credentials.c.account_id.asc(),
        )
        with self.engine.connect() as conn:
            rows = conn.execute(query).all()
            return [_row_to_status(row) for row in rows]

    def list_connections(self, household_id: UUID) -> dict[str, ConnectorStatus]:
        results: dict[str, ConnectorStatus] = {}
        for status in self.list_credentials(household_id):
            existing = results.get(status.provider_id)
            if existing is None or (existing.is_expired and not status.is_expired):
                results[status.provider_id] = status
        return results
