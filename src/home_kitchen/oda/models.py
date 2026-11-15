"""Display-only Oda product fields from the official product_search schema."""

from pydantic import BaseModel, Field, HttpUrl


class OdaAvailability(BaseModel):
    is_available: bool = Field(alias="isAvailable")
    description: str
    code: str


class OdaProduct(BaseModel):
    id: int
    name: str
    description: str
    price: str
    unit_price: str = Field(alias="unitPrice")
    unit_name: str = Field(alias="unitName")
    brand: str | None = None
    brand_id: int | None = Field(default=None, alias="brandId")
    url: HttpUrl | None = None
    availability: OdaAvailability | None = None
    # The manifest supplies neither images nor numeric pack content. A shopping
    # need's fulfillment quantity/unit must be reviewed separately by the user.


class OdaSearchGroup(BaseModel):
    query: str
    has_more: bool = Field(alias="hasMore")
    products: list[OdaProduct] = Field(max_length=20)


class OdaSearchResponse(BaseModel):
    result: list[OdaSearchGroup] = Field(max_length=10)
