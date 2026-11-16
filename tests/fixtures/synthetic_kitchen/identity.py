"""Original fictional canonical product, unit, and alias fixtures.

Every identifier is synthetic and deterministic. These fixtures satisfy the
catalogue identity model (#98) without importing household data or captured
provider catalogues.
"""

from copy import deepcopy
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from home_kitchen.catalogue.identity import CanonicalProduct, CanonicalUnit

# Synthetic canonical product IDs
SYNTHETIC_CARROT_PRODUCT_ID = UUID("99000001-0000-0000-0000-000000000001")
SYNTHETIC_BARLEY_PRODUCT_ID = UUID("99000001-0000-0000-0000-000000000002")
SYNTHETIC_CHICKPEAS_PRODUCT_ID = UUID("99000001-0000-0000-0000-000000000003")
SYNTHETIC_CITRUS_OIL_PRODUCT_ID = UUID("99000001-0000-0000-0000-000000000004")
SYNTHETIC_OAT_DRINK_PRODUCT_ID = UUID("99000001-0000-0000-0000-000000000005")
SYNTHETIC_COFFEE_PRODUCT_ID = UUID("99000001-0000-0000-0000-000000000006")
SYNTHETIC_COFFEE_WHITENER_PRODUCT_ID = UUID("99000001-0000-0000-0000-000000000007")
SYNTHETIC_TOMATO_FRESH_PRODUCT_ID = UUID("99000001-0000-0000-0000-000000000008")
SYNTHETIC_TOMATO_TINNED_PRODUCT_ID = UUID("99000001-0000-0000-0000-000000000009")

# Synthetic canonical unit IDs
SYNTHETIC_GRAM_UNIT_ID = UUID("99000002-0000-0000-0000-000000000001")
SYNTHETIC_TBSP_UNIT_ID = UUID("99000002-0000-0000-0000-000000000002")
SYNTHETIC_LITER_UNIT_ID = UUID("99000002-0000-0000-0000-000000000003")
SYNTHETIC_ITEM_UNIT_ID = UUID("99000002-0000-0000-0000-000000000004")


class SyntheticAlias(BaseModel):
    """Synthetic alias record for testing language and context scoped resolution."""

    kind: Literal["product", "unit"]
    locale: str
    context: str = ""
    alias: str
    identity_id: UUID
    reviewed: bool = True


class SyntheticAliasScenario(BaseModel):
    """Test scenario for alias resolution and reviewed correction behavior."""

    name: str
    kind: Literal["product", "unit"]
    locale: str
    context: str = ""
    query_alias: str
    expected_status: Literal["resolved", "unmapped", "ambiguous"]
    expected_identity_ids: list[UUID] = Field(default_factory=list)


def synthetic_canonical_products() -> list[CanonicalProduct]:
    """Return fresh copies of canonical product identity fixtures."""
    return deepcopy(
        [
            CanonicalProduct(id=SYNTHETIC_CARROT_PRODUCT_ID, canonical_name="carrot", variant=""),
            CanonicalProduct(
                id=SYNTHETIC_BARLEY_PRODUCT_ID, canonical_name="pearl barley", variant=""
            ),
            CanonicalProduct(
                id=SYNTHETIC_CHICKPEAS_PRODUCT_ID,
                canonical_name="chickpeas",
                variant="canned",
            ),
            CanonicalProduct(
                id=SYNTHETIC_CITRUS_OIL_PRODUCT_ID,
                canonical_name="citrus oil",
                variant="",
            ),
            CanonicalProduct(
                id=SYNTHETIC_OAT_DRINK_PRODUCT_ID,
                canonical_name="oat drink",
                variant="",
            ),
            CanonicalProduct(
                id=SYNTHETIC_COFFEE_PRODUCT_ID, canonical_name="coffee", variant="ground"
            ),
            CanonicalProduct(
                id=SYNTHETIC_COFFEE_WHITENER_PRODUCT_ID,
                canonical_name="coffee whitener",
                variant="",
            ),
            CanonicalProduct(
                id=SYNTHETIC_TOMATO_FRESH_PRODUCT_ID,
                canonical_name="tomato",
                variant="fresh",
            ),
            CanonicalProduct(
                id=SYNTHETIC_TOMATO_TINNED_PRODUCT_ID,
                canonical_name="tomato",
                variant="tinned",
            ),
        ]
    )


def synthetic_canonical_units() -> list[CanonicalUnit]:
    """Return fresh copies of canonical unit identity fixtures."""
    return deepcopy(
        [
            CanonicalUnit(id=SYNTHETIC_GRAM_UNIT_ID, canonical_name="g"),
            CanonicalUnit(id=SYNTHETIC_TBSP_UNIT_ID, canonical_name="tbsp"),
            CanonicalUnit(id=SYNTHETIC_LITER_UNIT_ID, canonical_name="l"),
            CanonicalUnit(id=SYNTHETIC_ITEM_UNIT_ID, canonical_name="item"),
        ]
    )


def synthetic_reviewed_aliases() -> list[SyntheticAlias]:
    """Return verified English/Bokmål alias mappings across recipe and provider contexts."""
    return deepcopy(
        [
            # Product aliases: Bokmål and English, recipe and Oda contexts
            SyntheticAlias(
                kind="product",
                locale="nb",
                context="recipe",
                alias="gulrot",
                identity_id=SYNTHETIC_CARROT_PRODUCT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="product",
                locale="en",
                context="recipe",
                alias="carrot",
                identity_id=SYNTHETIC_CARROT_PRODUCT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="product",
                locale="nb",
                context="oda",
                alias="gulrotbånd",
                identity_id=SYNTHETIC_CARROT_PRODUCT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="product",
                locale="nb",
                context="recipe",
                alias="byggryn",
                identity_id=SYNTHETIC_BARLEY_PRODUCT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="product",
                locale="en",
                context="recipe",
                alias="pearl barley",
                identity_id=SYNTHETIC_BARLEY_PRODUCT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="product",
                locale="nb",
                context="recipe",
                alias="kikerter",
                identity_id=SYNTHETIC_CHICKPEAS_PRODUCT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="product",
                locale="en",
                context="recipe",
                alias="chickpeas",
                identity_id=SYNTHETIC_CHICKPEAS_PRODUCT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="product",
                locale="nb",
                context="oda",
                alias="havredrikk",
                identity_id=SYNTHETIC_OAT_DRINK_PRODUCT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="product",
                locale="en",
                context="recipe",
                alias="oat drink",
                identity_id=SYNTHETIC_OAT_DRINK_PRODUCT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="product",
                locale="nb",
                context="recipe",
                alias="kaffe",
                identity_id=SYNTHETIC_COFFEE_PRODUCT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="product",
                locale="en",
                context="recipe",
                alias="coffee",
                identity_id=SYNTHETIC_COFFEE_PRODUCT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="product",
                locale="en",
                context="oda",
                alias="coffee whitener",
                identity_id=SYNTHETIC_COFFEE_WHITENER_PRODUCT_ID,
                reviewed=True,
            ),
            # Unit aliases: Bokmål and English, recipe and Oda contexts
            SyntheticAlias(
                kind="unit",
                locale="en",
                context="recipe",
                alias="g",
                identity_id=SYNTHETIC_GRAM_UNIT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="unit",
                locale="nb",
                context="recipe",
                alias="gram",
                identity_id=SYNTHETIC_GRAM_UNIT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="unit",
                locale="en",
                context="recipe",
                alias="tbsp",
                identity_id=SYNTHETIC_TBSP_UNIT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="unit",
                locale="nb",
                context="recipe",
                alias="ss",
                identity_id=SYNTHETIC_TBSP_UNIT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="unit",
                locale="en",
                context="recipe",
                alias="item",
                identity_id=SYNTHETIC_ITEM_UNIT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="unit",
                locale="nb",
                context="oda",
                alias="stk",
                identity_id=SYNTHETIC_ITEM_UNIT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="unit",
                locale="en",
                context="recipe",
                alias="l",
                identity_id=SYNTHETIC_LITER_UNIT_ID,
                reviewed=True,
            ),
            SyntheticAlias(
                kind="unit",
                locale="nb",
                context="recipe",
                alias="liter",
                identity_id=SYNTHETIC_LITER_UNIT_ID,
                reviewed=True,
            ),
        ]
    )


def synthetic_unreviewed_aliases() -> list[SyntheticAlias]:
    """Return unreviewed/ambiguous alias mappings for testing conflict resolution."""
    return deepcopy(
        [
            SyntheticAlias(
                kind="product",
                locale="nb",
                context="recipe",
                alias="tomat",
                identity_id=SYNTHETIC_TOMATO_FRESH_PRODUCT_ID,
                reviewed=False,
            ),
            SyntheticAlias(
                kind="product",
                locale="nb",
                context="recipe",
                alias="tomat",
                identity_id=SYNTHETIC_TOMATO_TINNED_PRODUCT_ID,
                reviewed=False,
            ),
        ]
    )


def synthetic_alias_scenarios() -> list[SyntheticAliasScenario]:
    """Return test scenarios for resolution states: resolved, unmapped, ambiguous."""
    return deepcopy(
        [
            SyntheticAliasScenario(
                name="Bokmal carrot recipe alias resolves cleanly",
                kind="product",
                locale="nb",
                context="recipe",
                query_alias="gulrot",
                expected_status="resolved",
                expected_identity_ids=[SYNTHETIC_CARROT_PRODUCT_ID],
            ),
            SyntheticAliasScenario(
                name="English carrot recipe alias resolves cleanly",
                kind="product",
                locale="en",
                context="recipe",
                query_alias="Carrot",
                expected_status="resolved",
                expected_identity_ids=[SYNTHETIC_CARROT_PRODUCT_ID],
            ),
            SyntheticAliasScenario(
                name="Bokmal tablespoon unit alias resolves cleanly",
                kind="unit",
                locale="nb",
                context="recipe",
                query_alias="ss",
                expected_status="resolved",
                expected_identity_ids=[SYNTHETIC_TBSP_UNIT_ID],
            ),
            SyntheticAliasScenario(
                name="Unmapped alias returns unmapped status",
                kind="product",
                locale="en",
                context="recipe",
                query_alias="unmapped fictional ingredient",
                expected_status="unmapped",
                expected_identity_ids=[],
            ),
            SyntheticAliasScenario(
                name="Ambiguous unreviewed alias returns ambiguous status with multiple candidates",
                kind="product",
                locale="nb",
                context="recipe",
                query_alias="tomat",
                expected_status="ambiguous",
                expected_identity_ids=[
                    SYNTHETIC_TOMATO_FRESH_PRODUCT_ID,
                    SYNTHETIC_TOMATO_TINNED_PRODUCT_ID,
                ],
            ),
        ]
    )
