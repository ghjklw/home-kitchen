"""Central, secret-free descriptions of independently implemented connections."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, model_serializer

from home_kitchen.oda.service import OdaService
from home_kitchen.settings import Settings

if TYPE_CHECKING:
    from home_kitchen.connector_store import ConnectorStore

_REFRESHABLE_PROVIDERS = frozenset({"rema1000", "coop", "cookidoo", "trumf"})
_OAUTH_REFRESH_MODES = frozenset({"oauth_pkce", "oauth_password"})
_TRUMF_REFRESH_MODES = frozenset({"session_cookie", "interactive_otp", "manual_session_cookie"})


def _parse_optional_datetime(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=UTC) if val.tzinfo is None else val.astimezone(UTC)
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val)
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except ValueError:
            return None
    return None


class IntegrationAccountInfo(BaseModel):
    account_id: str
    account_label: str | None = None
    auth_mode: str
    auth_type: str
    status: Literal["connected", "expired"]
    connected_by_actor_id: str | None = None
    updated_by_actor_id: str | None = None
    expires_at: datetime | None = None
    token_expires_at: datetime | None = None
    token_status: Literal["connected", "expiring_soon", "expired"]
    expiry_prompt: str | None = None
    last_refreshed_at: datetime | None = None
    last_error: str | None = None
    refresh_available: bool = False


class IntegrationInfo(BaseModel):
    id: str
    name: str
    status: Literal["connected", "configured", "not_configured", "unavailable", "expired"]
    connected: bool | None = None
    auth_mode: str | None = None
    auth_type: str | None = None
    supported_auth_modes: list[str]
    capabilities: list[str]
    detail: str
    management_path: str = "/#settings"
    expires_at: datetime | None = None
    token_expires_at: datetime | None = None
    token_status: (
        Literal[
            "connected", "expiring_soon", "configured", "not_configured", "unavailable", "expired"
        ]
        | None
    ) = None
    last_error: str | None = None
    expiry_prompt: str | None = None
    account_count: int = 0
    accounts: list[IntegrationAccountInfo] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if self.connected is None and isinstance(data, dict):
            data.pop("connected", None)
        return data


class IntegrationProvider(Protocol):
    def describe(self, actor_id: str | None) -> IntegrationInfo: ...


class IntegrationRegistry:
    def __init__(self, providers: list[IntegrationProvider]):
        self.providers = providers

    def describe(self, actor_id: str | None) -> list[IntegrationInfo]:
        return [provider.describe(actor_id) for provider in self.providers]

    def describe_one(self, provider_id: str, actor_id: str | None) -> IntegrationInfo:
        for provider in self.providers:
            info = provider.describe(actor_id)
            if info.id == provider_id:
                return info
        raise KeyError(f"Integration provider '{provider_id}' not found")


class OdaIntegration:
    def __init__(self, oda: OdaService):
        self.oda = oda

    def describe(self, actor_id: str | None) -> IntegrationInfo:
        state = self.oda.status(actor_id or "")
        is_connected = bool(state["connected"])
        status: Literal["connected", "configured", "not_configured"] = (
            "connected"
            if is_connected
            else "configured"
            if state["configured"]
            else "not_configured"
        )
        auth_mode = "delegated_user_pkce" if is_connected else None
        accounts: list[IntegrationAccountInfo] = []
        if is_connected:
            accounts.append(
                IntegrationAccountInfo(
                    account_id=actor_id or "default",
                    account_label=actor_id or None,
                    auth_mode="delegated_user_pkce",
                    auth_type="delegated_user_pkce",
                    status="connected",
                    connected_by_actor_id=actor_id,
                    updated_by_actor_id=actor_id,
                    token_status="connected",
                    refresh_available=False,
                )
            )
        return IntegrationInfo(
            id="oda",
            name="Oda",
            status=status,
            connected=is_connected,
            auth_mode=auth_mode,
            auth_type=auth_mode,
            supported_auth_modes=["delegated_user_pkce"],
            capabilities=[
                "product_search",
                "recipe_collections",
                "cart_review",
                "confirmed_cart_additions",
            ],
            detail="Personal recipes and shopping require your Oda consent. Checkout and payment stay at Oda.",
            token_status=status,
            account_count=len(accounts),
            accounts=accounts,
        )


class AIIntegration:
    def __init__(self, settings: Settings):
        self.settings = settings

    def describe(self, actor_id: str | None) -> IntegrationInfo:
        mode = (
            "federation"
            if self.settings.anthropic_workload_identity
            else "api_key"
            if self.settings.anthropic_api_key
            else None
        )
        status: Literal["configured", "not_configured"] = (
            "configured" if self.settings.ai_configured else "not_configured"
        )
        return IntegrationInfo(
            id="anthropic",
            name="Recipe assistant",
            status=status,
            connected=None,
            auth_mode=mode,
            auth_type=mode,
            supported_auth_modes=["federation", "api_key"],
            capabilities=["source_adaptation", "photo_transcription", "meal_recommendations"],
            detail="Configured by the operator. Federation is preferred where supported; configuration does not prove a successful model call.",
            token_status=status,
            account_count=0,
            accounts=[],
        )


class StoredConnectorIntegration:
    def __init__(
        self,
        provider_id: str,
        name: str,
        supported_auth_modes: list[str],
        capabilities: list[str],
        detail: str,
        store: ConnectorStore | None = None,
        household_id: UUID | None = None,
        default_expiry_prompt: str | None = None,
    ):
        self.provider_id = provider_id
        self.name = name
        self.supported_auth_modes = supported_auth_modes
        self.capabilities = capabilities
        self.detail = detail
        self.store = store
        self.household_id = household_id
        self.default_expiry_prompt = default_expiry_prompt

    def _describe_account(self, conn_status: Any) -> IntegrationAccountInfo:
        auth_mode = str(conn_status.auth_mode)
        expires_at = getattr(conn_status, "expires_at", None)
        is_expired = bool(conn_status.is_expired)
        expiry_prompt: str | None = None

        if is_expired:
            acc_status: Literal["connected", "expired"] = "expired"
            token_status: Literal["connected", "expiring_soon", "expired"] = "expired"
            expiry_prompt = (
                self.default_expiry_prompt
                or f"Sesjonen for {self.name} har utløpt. Koble til på nytt."
            )
        else:
            acc_status = "connected"
            token_status = "connected"
            if expires_at is not None:
                diff = expires_at - datetime.now(UTC)
                diff_days = diff.total_seconds() / 86400.0
                if 0 <= diff_days <= 7:
                    token_status = "expiring_soon"
                    days = max(1, int(diff_days) + 1)
                    expiry_prompt = f"Sesjonen utløper om {days} {'dag' if days == 1 else 'dager'}."

        meta = getattr(conn_status, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        account_id = str(getattr(conn_status, "account_id", "default") or "default")
        if self.provider_id == "trumf" and auth_mode in _TRUMF_REFRESH_MODES:
            can_refresh = False
            if "has_session_cookie" in meta:
                can_refresh = bool(meta["has_session_cookie"])
            elif self.store is not None and self.household_id is not None:
                get_creds_fn = getattr(self.store, "get_credentials", None)
                if callable(get_creds_fn):
                    creds = get_creds_fn(
                        self.household_id,
                        self.provider_id,
                        account_id=account_id,
                    )
                    if isinstance(creds, dict):
                        raw_cookie = creds.get("session_cookie") or creds.get("cookie")
                        can_refresh = isinstance(raw_cookie, str) and bool(raw_cookie.strip())
        else:
            can_refresh = (
                self.provider_id in _REFRESHABLE_PROVIDERS and auth_mode in _OAUTH_REFRESH_MODES
            )
            if can_refresh and "has_refresh_token" in meta:
                can_refresh = bool(meta["has_refresh_token"])

        last_err_raw = meta.get("last_error")
        last_error = str(last_err_raw) if isinstance(last_err_raw, str) and last_err_raw else None
        connected_by = getattr(conn_status, "connected_by_actor_id", None) or getattr(
            conn_status, "actor_id", None
        )
        updated_by = getattr(conn_status, "updated_by_actor_id", None) or getattr(
            conn_status, "actor_id", None
        )

        return IntegrationAccountInfo(
            account_id=account_id,
            account_label=getattr(conn_status, "account_label", None),
            auth_mode=auth_mode,
            auth_type=auth_mode,
            status=acc_status,
            connected_by_actor_id=connected_by,
            updated_by_actor_id=updated_by,
            expires_at=expires_at,
            token_expires_at=expires_at,
            token_status=token_status,
            expiry_prompt=expiry_prompt,
            last_refreshed_at=_parse_optional_datetime(meta.get("last_refreshed_at")),
            last_error=last_error,
            refresh_available=can_refresh,
        )

    def describe(self, actor_id: str | None) -> IntegrationInfo:
        status: Literal["connected", "configured", "not_configured", "unavailable", "expired"] = (
            "not_configured"
        )
        token_status: Literal[
            "connected", "expiring_soon", "configured", "not_configured", "unavailable", "expired"
        ] = "not_configured"
        auth_mode: str | None = None
        expires_at: datetime | None = None
        expiry_prompt: str | None = None
        last_error: str | None = None
        accounts: list[IntegrationAccountInfo] = []

        if self.store is not None and self.household_id is not None:
            list_fn = getattr(self.store, "list_credentials", None)
            if callable(list_fn):
                raw_statuses = list_fn(self.household_id, self.provider_id)
            else:
                single = self.store.get_connection_metadata(self.household_id, self.provider_id)
                raw_statuses = [single] if single is not None else []
            accounts = [self._describe_account(conn_status) for conn_status in raw_statuses]

        if accounts:
            active_accounts = [a for a in accounts if a.status == "connected"]
            if active_accounts:
                status = "connected"
                primary = active_accounts[0]
            else:
                status = "expired"
                primary = accounts[0]
            auth_mode = primary.auth_mode
            expires_at = primary.expires_at
            token_status = primary.token_status
            last_error = primary.last_error or next(
                (a.last_error for a in accounts if a.last_error), None
            )
            if status == "expired":
                expiry_prompt = primary.expiry_prompt
            elif any(a.status == "expired" for a in accounts):
                expiry_prompt = (
                    "Én eller flere tilkoblede kontoer har utløpt. Forny dem i innstillingene."
                )
            else:
                expiry_prompt = primary.expiry_prompt or next(
                    (a.expiry_prompt for a in accounts if a.expiry_prompt), None
                )

        return IntegrationInfo(
            id=self.provider_id,
            name=self.name,
            status=status,
            connected=(status == "connected"),
            auth_mode=auth_mode,
            auth_type=auth_mode,
            supported_auth_modes=self.supported_auth_modes,
            capabilities=self.capabilities,
            detail=self.detail,
            expires_at=expires_at,
            token_expires_at=expires_at,
            token_status=token_status,
            last_error=last_error,
            expiry_prompt=expiry_prompt,
            account_count=len(accounts),
            accounts=accounts,
        )


class Rema1000Integration(StoredConnectorIntegration):
    def __init__(
        self,
        store: ConnectorStore | None = None,
        household_id: UUID | None = None,
    ):
        super().__init__(
            provider_id="rema1000",
            name="Rema 1000",
            supported_auth_modes=["oauth_pkce", "session_token"],
            capabilities=["purchase_history", "receipt_sync"],
            detail="Koble til Rema 1000 Æ med sesjonstoken for automatisk synkronisering av kjøpshistorikk og rabatter.",
            store=store,
            household_id=household_id,
            default_expiry_prompt="Sesjonen for Rema 1000 har utløpt. Oppdater sesjonstokenet for å fortsette synkronisering.",
        )


class CoopIntegration(StoredConnectorIntegration):
    def __init__(
        self,
        store: ConnectorStore | None = None,
        household_id: UUID | None = None,
    ):
        super().__init__(
            provider_id="coop",
            name="Coop",
            supported_auth_modes=["oauth_pkce", "session_token", "session_cookie"],
            capabilities=["purchase_history", "receipt_sync"],
            detail="Koble til Coop med sesjonstoken eller informasjonskapsel for automatisk kvitterings- og kjøpshistorikk.",
            store=store,
            household_id=household_id,
            default_expiry_prompt="Sesjonen for Coop har utløpt. Koble til på nytt med et gyldig token eller informasjonskapsel.",
        )


class TrumfIntegration(StoredConnectorIntegration):
    def __init__(
        self,
        store: ConnectorStore | None = None,
        household_id: UUID | None = None,
    ):
        super().__init__(
            provider_id="trumf",
            name="Trumf",
            supported_auth_modes=["interactive_otp", "session_cookie"],
            capabilities=["purchase_history", "receipt_sync"],
            detail="Koble til Trumf med sesjonsinformasjonskapsel for automatisk kvitterings- og kjøpshistorikk.",
            store=store,
            household_id=household_id,
            default_expiry_prompt="Sesjonen for Trumf har utløpt. Oppdater informasjonskapselen for å fortsette synkronisering.",
        )


class CookidooIntegration(StoredConnectorIntegration):
    def __init__(
        self,
        store: ConnectorStore | None = None,
        household_id: UUID | None = None,
    ):
        super().__init__(
            provider_id="cookidoo",
            name="Cookidoo / Thermomix TM6",
            supported_auth_modes=["oauth_password", "session_cookie", "credentials"],
            capabilities=["recipe_import", "meal_planning"],
            detail="Uoffisiell tilkobling til Cookidoo / TM6 for oppskriftsimport og ukeplansynkronisering.",
            store=store,
            household_id=household_id,
            default_expiry_prompt="Tilkoblingen til Cookidoo har utløpt. Koble til på nytt for å synkronisere oppskrifter.",
        )
