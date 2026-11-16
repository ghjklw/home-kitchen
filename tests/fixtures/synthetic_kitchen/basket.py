"""Original fictional basket candidate and matching scenarios.

Every product, label, and URL is synthetic. Candidates cover identity confirmation
states (`compatible`, `unverified`, `incompatible`) and excluded negative examples (#97/#100).
"""

from copy import deepcopy
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, HttpUrl

from home_kitchen.basket.matching import (
    BasketCandidate,
    NeedMatchResult,
    ProviderProduct,
)

SYNTHETIC_BASKET_NEED_ID = UUID("99000003-0000-0000-0000-000000000001")
SYNTHETIC_UNVERIFIED_NEED_ID = UUID("99000003-0000-0000-0000-000000000002")


class SyntheticBasketMatchScenario(BaseModel):
    """Scenario verifying query expansion, identity confirmation status, and negative exclusion."""

    scenario_id: str
    query: str
    product: ProviderProduct
    expected_identity_status: Literal["compatible", "unverified", "incompatible"]
    is_excluded: bool
    requires_confirmation: bool
    description: str


def synthetic_compatible_candidate() -> BasketCandidate:
    """Return a candidate whose identity is verified compatible with its query."""
    return BasketCandidate(
        id="oda:9900002",
        provider="oda",
        product_id="9900002",
        name="TEST ONLY Gulrotbånd, 2 stk",
        description="Fictional fixture product; not a sale listing.",
        price="18.50",
        unit_price="9.25",
        unit_name="per stk",
        brand="SYNTHETIC FIXTURE",
        product_url=HttpUrl("https://fixtures.invalid/oda/products/9900002"),
        pack_description="2 stk",
        pack_quantity=Decimal("2"),
        pack_unit="item",
        pack_metadata_source="product_page",
        pack_metadata_status="unverified",
        match_query="gulrot",
        match_reason="Identity compatible; returned by oda product search for the need name 'gulrot'.",
        identity_status="compatible",
        recommended_packages=1,
        recommended_fulfillment_quantity=Decimal("2"),
    )


def synthetic_unverified_candidate() -> BasketCandidate:
    """Return a candidate requiring explicit human confirmation before batch selection."""
    return BasketCandidate(
        id="oda:9900003",
        provider="oda",
        product_id="9900003",
        name="TEST ONLY Citrus Oil Shower Gel",
        description="Fictional fixture cosmetic product.",
        price="45.00",
        unit_price="45.00",
        unit_name="per stk",
        brand="SYNTHETIC FIXTURE",
        product_url=HttpUrl("https://fixtures.invalid/oda/products/9900003"),
        pack_description="1 stk",
        pack_quantity=Decimal("1"),
        pack_unit="item",
        pack_metadata_source="product_page",
        pack_metadata_status="unverified",
        match_query="citrus oil shower gel",
        match_reason=(
            "Identity unverified; explicit confirmation required; "
            "returned by oda product search for the need name 'citrus oil shower gel'."
        ),
        identity_status="unverified",
        recommended_packages=1,
        recommended_fulfillment_quantity=Decimal("1"),
    )


def synthetic_incompatible_unit_candidate() -> BasketCandidate:
    """Return a candidate with a dimensionally incompatible packaging unit."""
    return BasketCandidate(
        id="oda:9900004",
        provider="oda",
        product_id="9900004",
        name="TEST ONLY Incompatible liquid bulk pack",
        description="Packaged in volume for a mass-based need.",
        price="120.00",
        unit_price="120.00",
        unit_name="per l",
        brand="SYNTHETIC FIXTURE",
        product_url=HttpUrl("https://fixtures.invalid/oda/products/9900004"),
        pack_description="1 l",
        pack_quantity=Decimal("1"),
        pack_unit="l",
        pack_metadata_source="product_page",
        pack_metadata_status="unverified",
        match_query="pearl barley",
        match_reason="Identity unverified; returned by search.",
        identity_status="unverified",
    )


def synthetic_basket_need_match(
    need_id: UUID | None = None,
    candidate: BasketCandidate | None = None,
    name: str = "gulrot",
    quantity: Decimal = Decimal("2"),
    unit: str = "item",
) -> NeedMatchResult:
    """Return a NeedMatchResult envelope wrapping fixture candidates."""
    resolved_candidate = candidate or synthetic_compatible_candidate()
    return NeedMatchResult(
        need_id=need_id or SYNTHETIC_BASKET_NEED_ID,
        name=name,
        quantity=quantity,
        unit=unit,
        page=1,
        has_more=False,
        candidates=[resolved_candidate],
    )


def synthetic_batch_selection(
    need_id: UUID,
    candidate_id: str,
    *,
    confirm_identity: bool | None = None,
    packages: int = 1,
) -> dict[str, Any]:
    """Build a selection payload dictionary for batch candidate selection."""
    entry: dict[str, Any] = {
        "need_id": str(need_id),
        "candidate_id": candidate_id,
        "packages": packages,
    }
    if confirm_identity is not None:
        entry["confirm_identity"] = confirm_identity
    return entry


def synthetic_basket_match_scenarios() -> list[SyntheticBasketMatchScenario]:
    """Return test scenarios for identity compatibility, unverified matches, and exclusions."""
    return deepcopy(
        [
            # Compatible matches
            SyntheticBasketMatchScenario(
                scenario_id="compatible-ground-coffee",
                query="ground coffee",
                product=ProviderProduct(
                    provider="oda",
                    product_id="9900005",
                    name="TEST ONLY Filterkaffe, 250 g",
                    description="Fictional ground filter coffee",
                    brand="SYNTHETIC FIXTURE",
                ),
                expected_identity_status="compatible",
                is_excluded=False,
                requires_confirmation=False,
                description="Ground coffee query matches Filterkaffe as compatible.",
            ),
            SyntheticBasketMatchScenario(
                scenario_id="compatible-olive-oil",
                query="olive oil",
                product=ProviderProduct(
                    provider="oda",
                    product_id="9900011",
                    name="TEST ONLY Ekstra jomfru olivenolje, 500 ml",
                    description="Fictional extra virgin olive oil",
                    brand="SYNTHETIC FIXTURE",
                ),
                expected_identity_status="compatible",
                is_excluded=False,
                requires_confirmation=False,
                description="Olive oil query matches olivenolje product as compatible.",
            ),
            # Unverified matches: manual intent or unmodeled semantic forms (#97)
            SyntheticBasketMatchScenario(
                scenario_id="unverified-manual-non-food",
                query="olive oil shower gel",
                product=ProviderProduct(
                    provider="oda",
                    product_id="9900003",
                    name="TEST ONLY Olive Oil Shower Gel",
                    description="Cosmetic shower gel",
                    brand="SYNTHETIC FIXTURE",
                ),
                expected_identity_status="unverified",
                is_excluded=False,
                requires_confirmation=True,
                description="Manual non-food intent stays visible as unverified.",
            ),
            SyntheticBasketMatchScenario(
                scenario_id="unverified-semantic-form",
                query="dried tomatoes",
                product=ProviderProduct(
                    provider="oda",
                    product_id="9900006",
                    name="TEST ONLY Tomater, ferske",
                    description="Fresh tomatoes",
                    brand="SYNTHETIC FIXTURE",
                ),
                expected_identity_status="unverified",
                is_excluded=False,
                requires_confirmation=True,
                description="Unmodeled semantic form qualifier stays unverified.",
            ),
            SyntheticBasketMatchScenario(
                scenario_id="unverified-unmapped-pantry",
                query="unmapped fictional ingredient",
                product=ProviderProduct(
                    provider="oda",
                    product_id="9900007",
                    name="TEST ONLY Mystery item",
                    description="Unmapped pantry term",
                    brand="SYNTHETIC FIXTURE",
                ),
                expected_identity_status="unverified",
                is_excluded=False,
                requires_confirmation=True,
                description="Unmapped search terms remain unverified.",
            ),
            # Incompatible / negative excluded examples (#97)
            SyntheticBasketMatchScenario(
                scenario_id="incompatible-coffee-whitener",
                query="coffee",
                product=ProviderProduct(
                    provider="oda",
                    product_id="9900008",
                    name="TEST ONLY Coffee Whitener",
                    description="Coffee creamer / whitener product",
                    brand="SYNTHETIC FIXTURE",
                ),
                expected_identity_status="incompatible",
                is_excluded=True,
                requires_confirmation=False,
                description="Coffee whitener is excluded from coffee matches.",
            ),
            SyntheticBasketMatchScenario(
                scenario_id="incompatible-whole-beans-for-ground",
                query="ground coffee",
                product=ProviderProduct(
                    provider="oda",
                    product_id="9900009",
                    name="TEST ONLY Kaffebønner",
                    description="Whole coffee beans",
                    brand="SYNTHETIC FIXTURE",
                ),
                expected_identity_status="incompatible",
                is_excluded=True,
                requires_confirmation=False,
                description="Whole beans are excluded when ground coffee is requested.",
            ),
            SyntheticBasketMatchScenario(
                scenario_id="incompatible-sunflower-for-olive-oil",
                query="olive oil",
                product=ProviderProduct(
                    provider="oda",
                    product_id="9900010",
                    name="TEST ONLY Solsikkeolje, 1 l",
                    description="Sunflower oil is incompatible with olive oil query",
                    brand="SYNTHETIC FIXTURE",
                ),
                expected_identity_status="incompatible",
                is_excluded=True,
                requires_confirmation=False,
                description="Sunflower oil variant is excluded from olive oil queries.",
            ),
            SyntheticBasketMatchScenario(
                scenario_id="incompatible-chocolate-for-eggs",
                query="eggs",
                product=ProviderProduct(
                    provider="oda",
                    product_id="9900012",
                    name="TEST ONLY Melkesjokolade",
                    description="Chocolate product returned under egg search",
                    brand="SYNTHETIC FIXTURE",
                ),
                expected_identity_status="incompatible",
                is_excluded=True,
                requires_confirmation=False,
                description="Unrelated chocolate family is excluded from egg queries.",
            ),
        ]
    )
