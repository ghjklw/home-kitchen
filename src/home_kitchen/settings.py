"""Runtime configuration; secrets are injected by External Secrets, never persisted here."""

from ipaddress import ip_network
from pathlib import Path
from typing import Annotated
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
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Household(BaseModel):
    name: Text = ""
    adults: int = Field(default=1, ge=0, le=30)
    children_ages: list[Annotated[int, Field(ge=0, le=17)]] = Field(default_factory=list)
    dietary_preferences: list[Text] = Field(default_factory=list)
    default_servings: int = Field(default=2, ge=1, le=100)
    language: Text = "en"


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

    @property
    def ai_configured(self) -> bool:
        return bool(self.ai_model and (self.anthropic_api_key or self.anthropic_workload_identity))

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
        if self.public_url is not None and (
            self.public_url.scheme != "https"
            or self.public_url.username is not None
            or self.public_url.password is not None
            or self.public_url.query is not None
            or self.public_url.fragment is not None
            or self.public_url.path not in (None, "/")
        ):
            raise ValueError("public_url must be an HTTPS origin without credentials or a path")
        if self.oda_encryption_key is not None:
            try:
                Fernet(self.oda_encryption_key.get_secret_value().encode())
            except (ValueError, TypeError):
                raise ValueError("oda_encryption_key must be a valid Fernet key") from None
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
