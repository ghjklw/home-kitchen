"""Runtime configuration; secrets are injected by External Secrets, never persisted here."""

from ipaddress import ip_network
from pathlib import Path
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, StringConstraints, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Household(BaseModel):
    name: Text = ""
    adults: int = Field(default=1, ge=0, le=30)
    children_ages: list[Annotated[int, Field(ge=0, le=17)]] = Field(default_factory=list)
    dietary_preferences: list[Text] = Field(default_factory=list)
    default_servings: int = Field(default=2, ge=1, le=100)
    language: Text = "en"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KITCHEN_", extra="ignore")

    database_path: Path = Path("data/kitchen.sqlite3")
    local_dev: bool = False
    entra_tenant_id: UUID | None = None
    entra_audience: str | None = None
    entra_allowed_group: UUID | None = None
    entra_scope: str = "Kitchen.Access"
    # Trust these headers only behind the staged NetworkPolicy + Entra forwardAuth.
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    ai_model: str | None = None
    anthropic_api_key: SecretStr | None = None

    @model_validator(mode="after")
    def validate_auth(self) -> "Settings":
        if not self.local_dev and not all(
            [self.entra_tenant_id, self.entra_audience, self.entra_allowed_group]
        ):
            raise ValueError("Configure Entra tenant, audience and household group, or local_dev")
        for cidr in self.trusted_proxy_cidrs:
            network = ip_network(cidr)
            if network.prefixlen == 0:
                raise ValueError("A trusted proxy range cannot include the whole internet")
        return self
