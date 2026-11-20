"""Runtime configuration; secrets are injected by External Secrets, never persisted here."""

from ipaddress import ip_network
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet
from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    HttpUrl,
    PrivateAttr,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
DEFAULT_SHOPPING_CATEGORY_ORDER = (
    "Produce",
    "Bakery",
    "Meat",
    "Seafood",
    "Fish",
    "Dairy",
    "Chilled",
    "Pantry",
    "Spices",
    "Drinks",
    "Household",
    "Frozen",
    "Other",
)


class Household(BaseModel):
    name: Text = ""
    adults: int = Field(default=1, ge=0, le=30)
    children_ages: list[Annotated[int, Field(ge=0, le=17)]] = Field(default_factory=list)
    dietary_preferences: list[Text] = Field(default_factory=list)
    default_servings: int = Field(default=2, ge=1, le=100)
    language: Text = "nb"
    shopping_category_order: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    ] = Field(
        default_factory=lambda: list(DEFAULT_SHOPPING_CATEGORY_ORDER),
        max_length=64,
        description=(
            "Shopping category display order; unlisted categories follow alphabetically. "
            "An explicit empty list uses alphabetical order for all categories. "
            "Source category labels are preserved."
        ),
    )

    @field_validator("shopping_category_order")
    @classmethod
    def unique_shopping_categories(cls, value: list[str]) -> list[str]:
        if len({category.casefold() for category in value}) != len(value):
            raise ValueError("Shopping categories must be unique ignoring case")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KITCHEN_", extra="ignore", hide_input_in_errors=True
    )

    database_url: SecretStr

    local_dev: bool = False
    entra_tenant_id: UUID | None = None
    entra_audience: str | None = None
    entra_allowed_group: UUID | None = None
    entra_scope: str = "Kitchen.Access"
    mcp_oauth_enabled: bool = False
    mcp_public_url: HttpUrl | None = None
    mcp_client_secret: SecretStr | None = None
    mcp_signing_key: SecretStr | None = None
    mcp_encryption_key: SecretStr | None = None
    mcp_allowed_redirect_uris: list[str] = Field(
        default_factory=lambda: [
            "https://claude.ai/api/mcp/auth_callback",
            "https://chatgpt.com/connector/oauth/*",
            "https://chatgpt.com/connector_platform_oauth_redirect",
        ]
    )
    # Trust these headers only behind the staged NetworkPolicy + Entra forwardAuth.
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    ai_model: Text | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_workload_identity: bool = False
    anthropic_federation_rule_id: Text | None = None
    anthropic_organization_id: UUID | None = None
    anthropic_service_account_id: Text | None = None
    anthropic_workspace_id: Text | None = None
    azure_tenant_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("AZURE_TENANT_ID", "azure_tenant_id")
    )
    azure_client_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("AZURE_CLIENT_ID", "azure_client_id")
    )
    azure_federated_token_file: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("AZURE_FEDERATED_TOKEN_FILE", "azure_federated_token_file"),
    )
    _ai_runtime: object | None = PrivateAttr(default=None)
    public_url: HttpUrl | None = None
    oda_encryption_key: SecretStr | None = None
    connector_encryption_key: SecretStr | None = None
    kassal_api_key: SecretStr | None = None
    home_assistant_url: HttpUrl | None = None
    home_assistant_token: SecretStr | None = None
    home_assistant_todo_entity: str | None = None

    @property
    def ai_configured(self) -> bool:
        return bool(self.ai_model and (self.anthropic_api_key or self.anthropic_workload_identity))

    @field_validator("public_url", mode="before")
    @classmethod
    def validate_http_loopback_hostname(cls, value):
        # HttpUrl normalizes abbreviated/numeric IPv4 hosts; require explicit loopback names.
        if isinstance(value, str):
            parsed = urlsplit(value)
            if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("HTTP public_url must use localhost, 127.0.0.1 or [::1]")
        return value

    @model_validator(mode="after")
    def validate_mcp_oauth(self) -> "Settings":
        configured = [
            self.mcp_public_url,
            self.mcp_client_secret,
            self.mcp_signing_key,
            self.mcp_encryption_key,
        ]
        if not self.mcp_oauth_enabled:
            if any(configured):
                raise ValueError("Enable mcp_oauth_enabled explicitly to configure MCP OAuth")
            return self
        if (
            self.local_dev
            or not all(configured)
            or not all(
                [
                    self.entra_tenant_id,
                    self.entra_audience,
                    self.entra_allowed_group,
                ]
            )
        ):
            raise ValueError(
                "MCP OAuth requires production Entra identity, URL and all three secrets"
            )
        url = self.mcp_public_url
        if url is None or (
            url.scheme != "https"
            or url.port != 443
            or url.username is not None
            or url.password is not None
            or url.query is not None
            or url.fragment is not None
            or url.path not in ("/mcp", "/mcp/")
        ):
            raise ValueError("mcp_public_url must be a canonical HTTPS URL ending in /mcp")
        try:
            UUID(str(self.entra_audience))
        except ValueError:
            raise ValueError(
                "MCP OAuth requires the Kitchen application client ID GUID audience"
            ) from None
        secrets = [
            secret.get_secret_value()
            for secret in (self.mcp_client_secret, self.mcp_signing_key, self.mcp_encryption_key)
            if secret is not None
        ]
        if any(not secret.strip() for secret in secrets) or len(set(secrets)) != 3:
            raise ValueError("MCP OAuth secrets must be nonempty and independent")
        if (
            self.mcp_signing_key is None
            or len(self.mcp_signing_key.get_secret_value().encode()) < 32
        ):
            raise ValueError("mcp_signing_key must contain at least 32 bytes")
        try:
            Fernet(secrets[2].encode())
        except (ValueError, TypeError):
            raise ValueError("mcp_encryption_key must be a valid Fernet key") from None
        if self.oda_encryption_key and self.oda_encryption_key.get_secret_value() in secrets:
            raise ValueError("MCP OAuth keys must be independent from Oda encryption")
        if (
            self.connector_encryption_key
            and self.connector_encryption_key.get_secret_value() in secrets
        ):
            raise ValueError("MCP OAuth keys must be independent from connector encryption")
        if not self.mcp_allowed_redirect_uris:
            raise ValueError("MCP OAuth requires an explicit client redirect allowlist")
        for redirect in self.mcp_allowed_redirect_uris:
            try:
                parsed = HttpUrl(redirect)
            except ValueError:
                raise ValueError("MCP client redirects must be explicit HTTPS URLs") from None
            if (
                parsed.scheme != "https"
                or parsed.port != 443
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment is not None
                or parsed.query is not None
                or ("*" in redirect and redirect != "https://chatgpt.com/connector/oauth/*")
            ):
                raise ValueError(
                    "MCP client redirects must be exact HTTPS URLs or the ChatGPT callback pattern"
                )
        return self

    @model_validator(mode="after")
    def validate_ai_credentials(self) -> "Settings":
        wif_fields = [
            self.anthropic_federation_rule_id,
            self.anthropic_organization_id,
            self.anthropic_service_account_id,
        ]
        if (
            self.anthropic_api_key is not None
            and not self.anthropic_api_key.get_secret_value().strip()
        ):
            raise ValueError("anthropic_api_key must not be blank")
        if self.kassal_api_key is not None and not self.kassal_api_key.get_secret_value().strip():
            raise ValueError("kassal_api_key must not be blank")
        if (
            self.home_assistant_token is not None
            and not self.home_assistant_token.get_secret_value().strip()
        ):
            raise ValueError("home_assistant_token must not be blank")
        if self.home_assistant_todo_entity is not None:
            cleaned_entity = self.home_assistant_todo_entity.strip()
            if not cleaned_entity:
                raise ValueError("home_assistant_todo_entity must not be blank")
            self.home_assistant_todo_entity = cleaned_entity
        if self.anthropic_workload_identity:
            if self.anthropic_api_key is not None:
                raise ValueError(
                    "Choose either Anthropic API key or workload identity authentication"
                )
            if not all(
                wif_fields
                + [self.azure_tenant_id, self.azure_client_id, self.azure_federated_token_file]
            ):
                raise ValueError(
                    "Workload identity requires federation rule, organization, service "
                    "account, Azure tenant, client and projected token file"
                )
            if (
                self.azure_federated_token_file
                and not self.azure_federated_token_file.is_absolute()
            ):
                raise ValueError("Azure projected token file must be an absolute path")
        elif any(wif_fields) or self.anthropic_workspace_id:
            raise ValueError(
                "Enable anthropic_workload_identity explicitly to configure federation"
            )
        return self

    @model_validator(mode="after")
    def validate_auth(self) -> "Settings":
        local_http_origin = (
            self.local_dev
            and self.public_url is not None
            and self.public_url.scheme == "http"
            and self.public_url.host in ("localhost", "127.0.0.1", "[::1]")
            and self.public_url.port is not None
            and 1 <= self.public_url.port <= 65535
        )
        if self.public_url is not None and (
            (self.public_url.scheme != "https" and not local_http_origin)
            or self.public_url.username is not None
            or self.public_url.password is not None
            or self.public_url.query is not None
            or self.public_url.fragment is not None
            or self.public_url.path not in (None, "/")
        ):
            raise ValueError(
                "public_url must be an HTTPS origin (or HTTP loopback with local_dev) "
                "without credentials or a path"
            )
        if self.oda_encryption_key is not None:
            try:
                Fernet(self.oda_encryption_key.get_secret_value().encode())
            except (ValueError, TypeError):
                raise ValueError("oda_encryption_key must be a valid Fernet key") from None
        if self.connector_encryption_key is not None:
            try:
                Fernet(self.connector_encryption_key.get_secret_value().encode())
            except (ValueError, TypeError):
                raise ValueError("connector_encryption_key must be a valid Fernet key") from None
        try:
            driver = make_url(self.database_url.get_secret_value()).drivername
        except ArgumentError:
            raise ValueError("database_url must be a valid PostgreSQL URL") from None
        if driver != "postgresql+psycopg":
            raise ValueError("database_url must use postgresql+psycopg")
        if not self.local_dev and not all(
            [self.entra_tenant_id, self.entra_audience, self.entra_allowed_group]
        ):
            raise ValueError("Configure Entra tenant, audience and household group, or local_dev")
        for cidr in self.trusted_proxy_cidrs:
            network = ip_network(cidr)
            if network.prefixlen == 0:
                raise ValueError("A trusted proxy range cannot include the whole internet")
        return self
