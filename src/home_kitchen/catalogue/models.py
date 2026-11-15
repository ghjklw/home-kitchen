"""Validated records for generic grocery foods, independent of retailer products."""

from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, Field, HttpUrl, StringConstraints, ValidationInfo, field_validator

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

MATVARETABELLEN_NAMESPACE = UUID("f7cbf658-58cf-4d92-bb25-d63c631c5f59")
MATVARETABELLEN_SOURCE = "matvaretabellen"
MATVARETABELLEN_VERSION = "2026"
MATVARETABELLEN_URL = "https://www.matvaretabellen.no/api/nb/foods.json"
MATVARETABELLEN_ATTRIBUTION = "Matvaretabellen 2026. Mattilsynet. www.matvaretabellen.no"


def food_catalogue_id(source_item_id: str) -> UUID:
    """Return the stable application ID for one upstream food ID."""
    return uuid5(MATVARETABELLEN_NAMESPACE, f"{MATVARETABELLEN_SOURCE}:nb:{source_item_id}")


class FoodCatalogueItem(BaseModel):
    id: UUID
    source: Literal["matvaretabellen"]
    source_item_id: NonBlank
    source_name: NonBlank
    display_name: NonBlank
    locale: Literal["nb"] = "nb"
    food_group_id: NonBlank
    category: NonBlank
    default_unit: NonBlank | None = None
    search_keywords: list[NonBlank] = Field(default_factory=list)
    source_version: Literal["2026"]
    source_url: HttpUrl
    source_attribution: Literal["Matvaretabellen 2026. Mattilsynet. www.matvaretabellen.no"]

    @field_validator("source_item_id")
    @classmethod
    def stable_id_matches_source(cls, value: str, info: ValidationInfo) -> str:
        item_id = info.data.get("id")
        if item_id is not None and item_id != food_catalogue_id(value):
            raise ValueError("id must be the deterministic ID for source_item_id")
        return value

    @field_validator("source_url")
    @classmethod
    def require_official_data_endpoint(cls, value: HttpUrl) -> HttpUrl:
        if str(value) != MATVARETABELLEN_URL:
            raise ValueError("source_url must identify the official Norwegian foods endpoint")
        return value
