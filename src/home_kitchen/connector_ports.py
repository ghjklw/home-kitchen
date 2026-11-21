"""Narrow Protocol ports and domain models for external connector credentials and status."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_ACCOUNT_ID = "default"


class ConnectorStatus(BaseModel):
    """Secret-free status and metadata snapshot for a household connector account."""

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


class ConnectorCredentialReader(Protocol):
    """Read-only port for inspecting connector metadata and decrypted credentials."""

    def get_connection_metadata(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str | None = None,
    ) -> ConnectorStatus | None: ...

    def get_credentials(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    def list_credentials(
        self,
        household_id: UUID,
        provider_id: str | None = None,
    ) -> list[ConnectorStatus]: ...


class ConnectorCredentialPort(ConnectorCredentialReader, Protocol):
    """Full port for reading, writing, deleting, and locking connector credentials."""

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
    ) -> ConnectorStatus: ...

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
    ) -> ConnectorStatus: ...

    def update_account_metadata(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str,
        metadata_updates: dict[str, Any],
        *,
        remove_keys: tuple[str, ...] = (),
    ) -> None: ...

    def delete_connection(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str | None = None,
    ) -> bool: ...

    def delete_account_credentials(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str,
    ) -> bool: ...

    def lock_connection_credentials(
        self,
        connection: Any,
        household_id: UUID,
        provider_id: str,
        account_id: str | None = None,
    ) -> tuple[datetime, datetime | None, dict[str, Any]] | None: ...


class OdaStatusPort(Protocol):
    """Narrow port for querying Oda connection configuration and actor status."""

    def status(self, actor_id: str) -> Mapping[str, Any]: ...
