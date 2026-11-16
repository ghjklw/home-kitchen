"""Read-only product matching for the basket review workflow.

Provider results stay as candidates until a person selects one through the
existing basket review flow. Product-page pack text is treated as unverified
metadata and only a complete, unambiguous measurement can support a quantity.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from starlette.concurrency import run_in_threadpool

from home_kitchen.inventory import InventoryService, PurchasePlan
from home_kitchen.oda.media import ProductMedia, fetch_product_media
from home_kitchen.oda.models import OdaSearchResponse
from home_kitchen.oda.service import OdaService
from home_kitchen.units import (
    canonicalize_quantity,
    compatible_units,
    multiply_integer_exact,
    normalize_unit,
)

MAX_NEEDS = 100
MAX_RESULTS_PER_NEED = 5
MAX_MEDIA_FETCHES = 30
MAX_CONCURRENT_SEARCHES = 3
MAX_CONCURRENT_MEDIA = 3
SEARCH_TIMEOUT_SECONDS = 15
MEDIA_TIMEOUT_SECONDS = 8
MEDIA_CACHE_TTL = timedelta(minutes=15)
MEDIA_CACHE_MAX_ENTRIES = 256
SEARCH_CACHE_TTL = timedelta(minutes=5)
SEARCH_EMPTY_CACHE_TTL = timedelta(seconds=20)
SEARCH_CACHE_MAX_ENTRIES = 512
ODA_QUERY_BATCH_SIZE = 5
# Search-only expansions for legacy aliases not yet covered by the reviewed
# identity table. These never establish product compatibility.
_LEGACY_SEARCH_EXPANSIONS = {
    "all-purpose flour": "hvetemel",
    "all purpose flour": "hvetemel",
    "plain flour": "hvetemel",
    "large eggs": "egg L",
    "large egg": "egg L",
    "unsalted butter": "usaltet smør",
    "whole milk": "helmelk",
    "granulated sugar": "sukker",
    "caster sugar": "sukker",
    "vanilla extract": "vaniljeekstrakt",
    "vanilla essence": "vaniljeessens",
    "cocoa powder": "kakaopulver",
    "hot water": "varmt vann",
    "chicken breast": "kyllingfilet",
    "chicken breasts": "kyllingfilet",
    "chicken thigh": "kyllinglår",
    "chicken thighs": "kyllinglår",
    "ground beef": "kjøttdeig",
    "minced beef": "kjøttdeig",
    "minced meat": "kjøttdeig",
    "crème fraîche": "crème fraîche",
    "creme fraiche": "crème fraîche",
    "sour cream": "rømme",
    "heavy cream": "kremfløte",
    "whipping cream": "kremfløte",
    "spring onion": "vårløk",
    "green onion": "vårløk",
    "red onion": "rødløk",
    "yellow onion": "gul løk",
    "cherry tomatoes": "cherrytomater",
    "coffee beans": "kaffebønner",
    "black pepper": "sort pepper",
    "olive oil": "olivenolje",
    "vegetable oil": "matolje",
    "coconut milk": "kokosmelk",
    "red bell pepper": "rød paprika",
    "bell pepper": "paprika",
    "tomatoes": "tomater",
    "tomato": "tomat",
    "potatoes": "poteter",
    "potato": "potet",
    "carrots": "gulrøtter",
    "carrot": "gulrot",
    "garlic": "hvitløk",
    "onions": "løk",
    "onion": "løk",
    "coffee": "kaffe",
    "milk": "melk",
    "butter": "smør",
    "flour": "hvetemel",
    "sugar": "sukker",
    "eggs": "egg",
    "egg": "egg",
    "lemon": "sitron",
    "parsley": "persille",
    "basil": "basilikum",
    "coriander": "koriander",
    "cucumber": "agurk",
    "spinach": "spinat",
    "salmon": "laks",
    "rice": "ris",
    "pasta": "pasta",
    "wheat flour": "hvetemel",
    "egg yolks": "eggeplomme",
    "egg whites": "eggehvite",
    "powdered sugar": "melis",
    "icing sugar": "melis",
    # Recognize canonical Bokmål terms as identity queries too. Mapping them
    # to themselves leaves search expansion unchanged.
    "hvetemel": "hvetemel",
    "usaltet smør": "usaltet smør",
    "helmelk": "helmelk",
    "lettmelk": "lettmelk",
    "sukker": "sukker",
    "ris": "ris",
    "basmatiris": "basmatiris",
    "olivenolje": "olivenolje",
}
_BOKMAL_QUALIFIERS = {
    "fresh": "ferske",
    "chopped": "hakket",
    "diced": "terninger",
    "minced": "finhakket",
    "ground": "malt",
    "whole": "hel",
    "frozen": "fryst",
    "canned": "hermetisk",
    "dried": "tørket",
    "smoked": "røkt",
}
_INGREDIENT_PHRASES = re.compile(
    r"(?<!\w)("
    + "|".join(re.escape(key) for key in sorted(_LEGACY_SEARCH_EXPANSIONS, key=len, reverse=True))
    + r")(?!\w)",
    re.I,
)
_QUALIFIER_WORDS = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(key) for key in _BOKMAL_QUALIFIERS) + r")(?!\w)",
    re.I,
)
_TRAILING_FORM_QUALIFIER = re.compile(
    r"^(.*?)(?:,\s*|\s+)(hakket|finhakket|terninger|malt|hel|fryst|hermetisk|tørket|røkt|"
    r"for dusting|plus extra for greasing|separated|to taste|as needed)$",
    re.I,
)
_RECIPE_INSTRUCTION_SUFFIX = re.compile(
    r"(?:,\s*|\s+)(?:plus\s+extra\s+for\s+greasing|for\s+greasing|for\s+dusting|"
    r"separated|to\s+taste|as\s+needed)\s*$",
    re.I,
)


@dataclass(frozen=True)
class _IdentityFamily:
    name: str
    # alias, requested variant, localized query
    queries: tuple[tuple[str, str | None, str | None], ...]
    # variant and product identity expression
    products: tuple[tuple[str, str], ...]
    generic_variants: tuple[str, ...] = ()
    priority: int = 0


# English and Bokmål aliases, expansions, supported product compounds and
# variant compatibility are defined together. Product-only families identify
# known adjacent classes that must not be treated as ingredients.
_IDENTITY_FAMILIES = (
    _IdentityFamily(
        "flour",
        (
            ("all-purpose flour", "wheat", "hvetemel"),
            ("all purpose flour", "wheat", "hvetemel"),
            ("plain flour", "wheat", "hvetemel"),
            ("wheat flour", "wheat", "hvetemel"),
            ("hvetemel", "wheat", None),
            ("flour", None, "hvetemel"),
        ),
        (
            ("almond", r"\b(?:almond flour|mandelmel)\b"),
            ("oat", r"\b(?:oat flour|havremel)\b"),
            ("wheat", r"\b(?:hvetemel|wheat flour|all[- ]purpose flour|plain flour)\b"),
            ("unspecified", r"\bflour\b"),
        ),
        ("wheat", "almond", "oat", "unspecified"),
        20,
    ),
    _IdentityFamily(
        "eggs",
        (
            ("large eggs", "large", "egg L"),
            ("large egg", "large", "egg L"),
            ("eggs", None, "egg"),
            ("egg", None, "egg"),
        ),
        (
            (
                "large",
                r"\b(?:egg|eggs)\b.*\b(?:large|store|size L|xl|m/l)\b|\b(?:large|store|xl|m/l)\b.*\b(?:egg|eggs)\b",
            ),
            ("medium", r"\b(?:egg|eggs)\b.*\b(?:medium|size M)\b"),
            ("unspecified", r"\b(?:egg|eggs)\b"),
        ),
        ("large", "medium", "unspecified"),
        20,
    ),
    _IdentityFamily(
        "butter",
        (
            ("unsalted butter", "unsalted", "usaltet smør"),
            ("usaltet smør", "unsalted", None),
            ("butter", None, "smør"),
            ("smør", None, None),
        ),
        (
            ("unsalted", r"(?=.*\b(?:usaltet|unsalted)\b)(?=.*\b(?:meierismør|smør|butter)\b)"),
            ("salted", r"(?=.*\b(?:saltet|salted)\b)(?=.*\b(?:meierismør|smør|butter)\b)"),
            ("unspecified", r"\b(?:meierismør|smør|butter)\b"),
        ),
        ("unsalted", "salted", "unspecified"),
        20,
    ),
    _IdentityFamily(
        "milk",
        (
            ("whole milk", "whole", "helmelk"),
            ("helmelk", "whole", None),
            ("low-fat milk", "low-fat", "lettmelk"),
            ("lettmelk", "low-fat", None),
            ("milk", None, "melk"),
            ("melk", None, None),
        ),
        (
            ("whole", r"\b(?:helmelk|whole\s+milk|wholemilk)\b"),
            (
                "low-fat",
                r"\b(?:lettmelk|skummetmelk|low[- ]fat milk|skimmed milk|semi[- ]skimmed milk)\b",
            ),
            ("unspecified", r"\b(?:milk|melk)\b"),
        ),
        ("whole", "low-fat", "unspecified"),
        20,
    ),
    _IdentityFamily(
        "sugar",
        (
            ("granulated sugar", "granulated", "sukker"),
            ("caster sugar", "caster", "sukker"),
            ("powdered sugar", "powdered", "melis"),
            ("icing sugar", "powdered", "melis"),
            ("sugar", None, "sukker"),
            ("sukker", None, None),
            ("melis", "powdered", None),
        ),
        (
            ("caster", r"\bcaster sugar\b"),
            ("powdered", r"\b(?:powdered sugar|icing sugar|melis)\b"),
            ("granulated", r"\b(?:granulated sugar|white sugar|sukker|sugar)\b"),
        ),
        ("granulated", "caster", "powdered"),
        20,
    ),
    _IdentityFamily(
        "cooking-oil",
        (
            ("olive oil", "olive", "olivenolje"),
            ("olivenolje", "olive", None),
            ("rapeseed oil", "rapeseed", "rapsolje"),
            ("rapsolje", "rapeseed", None),
            ("sunflower oil", "sunflower", "solsikkeolje"),
            ("solsikkeolje", "sunflower", None),
            ("vegetable oil", None, "matolje"),
            ("cooking oil", None, "matolje"),
            ("matolje", None, None),
        ),
        (
            ("olive", r"\b(?:olive oil|olivenolje)\b"),
            ("rapeseed", r"\b(?:rapeseed oil|rapsolje)\b"),
            ("sunflower", r"\b(?:sunflower oil|solsikkeolje)\b"),
            ("unspecified", r"\b(?:vegetable oil|cooking oil|matolje|planteolje|maisolje)\b"),
        ),
        ("olive", "rapeseed", "sunflower", "unspecified"),
        20,
    ),
    _IdentityFamily(
        "rice",
        (
            ("basmati rice", "basmati", "basmatiris"),
            ("basmatiris", "basmati", None),
            ("jasmine rice", "jasmine", "jasminris"),
            ("jasminris", "jasmine", None),
            ("rice", None, "ris"),
            ("ris", None, None),
        ),
        (
            ("basmati", r"\b(?:basmati rice|basmatiris)\b"),
            ("jasmine", r"\b(?:jasmine rice|jasminris)\b"),
            ("unspecified", r"\b(?:rice|ris)\b"),
        ),
        ("basmati", "jasmine", "unspecified"),
        20,
    ),
    _IdentityFamily(
        "tomato",
        (
            ("tomato paste", "processed", "tomatpuré"),
            ("tomato puree", "processed", "tomatpuré"),
            ("tomatpuré", "processed", None),
            ("fresh tomatoes", "fresh", "ferske tomater"),
            ("fresh tomato", "fresh", "fersk tomat"),
            ("cherry tomatoes", "fresh", "cherrytomater"),
            ("tomatoes", "fresh", "tomater"),
            ("tomato", "fresh", "tomat"),
            ("tomater", "fresh", None),
            ("tomat", "fresh", None),
        ),
        (
            ("processed", r"\b(?:tomato paste|tomato puree|tomatpur[eé]|tomatkonsentrat)\b"),
            ("fresh", r"\b(?:tomatoes|tomato|tomater|tomat|cherrytomater)\b"),
        ),
        ("fresh",),
        20,
    ),
    _IdentityFamily(
        "coffee",
        (
            ("ground coffee", "ground", "malt kaffe"),
            ("malt kaffe", "ground", None),
            ("coffee beans", "beans", "kaffebønner"),
            ("coffee", None, "kaffe"),
            ("kaffe", None, None),
        ),
        (
            ("beans", r"\b(?:coffee beans|kaffebønner)\b"),
            ("ground", r"\b(?:ground coffee|filter coffee|filterkaffe|malt kaffe)\b"),
            ("unspecified", r"\b(?:coffee|kaffe)\b"),
        ),
        ("beans", "ground", "unspecified"),
        20,
    ),
    _IdentityFamily(
        "vanilla-extract",
        (
            ("vanilla extract", "extract", "vaniljeekstrakt"),
            ("vanilla essence", "extract", "vaniljeessens"),
            ("vaniljeekstrakt", "extract", None),
            ("vaniljeessens", "extract", None),
        ),
        (
            (
                "extract",
                r"\b(?:vanilla extract|vanilla essence|vaniljeekstrakt|vaniljeessens|vaniljearoma)\b",
            ),
        ),
        ("extract",),
        20,
    ),
    _IdentityFamily(
        "cocoa-powder",
        (
            ("cocoa powder", "powder", "kakaopulver"),
            ("cacao powder", "powder", "kakaopulver"),
            ("kakaopulver", "powder", None),
        ),
        (("powder", r"\b(?:cocoa powder|cacao powder|kakaopulver)\b"),),
        ("powder",),
        20,
    ),
    _IdentityFamily(
        "hot-water",
        (
            ("hot water", "hot", "varmt vann"),
            ("varmt vann", "hot", None),
        ),
        (("hot", r"\b(?:hot water|varmt vann)\b"),),
        ("hot",),
        20,
    ),
    _IdentityFamily(
        "chicken",
        (
            ("chicken breast", "breast", "kyllingfilet"),
            ("chicken breasts", "breast", "kyllingfilet"),
            ("chicken thigh", "thigh", "kyllinglår"),
            ("chicken thighs", "thigh", "kyllinglår"),
            ("kyllingfilet", "breast", None),
            ("kyllinglår", "thigh", None),
        ),
        (
            ("breast", r"\b(?:chicken breast|chicken breasts|kyllingfilet)\b"),
            ("thigh", r"\b(?:chicken thigh|chicken thighs|kyllinglår)\b"),
        ),
        ("breast", "thigh"),
        20,
    ),
    _IdentityFamily(
        "creme-fraiche",
        (
            ("crème fraîche", None, "crème fraîche"),
            ("creme fraiche", None, "crème fraîche"),
        ),
        (("unspecified", r"\bcr[eè]me\s+fra[iî]che\b"),),
        ("unspecified",),
        20,
    ),
    _IdentityFamily("chocolate", (), (("unspecified", r"\b(?:chocolate|sjokolade)\b"),), (), 100),
    _IdentityFamily(
        "personal-care",
        (),
        (
            (
                "unspecified",
                r"\b(?:shower\s+(?:gel|oil)|dusjgel[eé]|body\s+wash|shampoo|såpe|soap|lotion)\b",
            ),
        ),
        (),
        100,
    ),
    _IdentityFamily(
        "sugar-decoration",
        (),
        (
            (
                "unspecified",
                r"\b(?:sugar\s+(?:decoration|decorations|faces)|sukkerpynt|dekorstrøssel|sprinkles)\b",
            ),
        ),
        (),
        100,
    ),
    _IdentityFamily("tortilla", (), (("unspecified", r"\b(?:tortillas?|wraps?)\b"),), (), 100),
    _IdentityFamily("diaper", (), (("unspecified", r"\b(?:diapers?|nappies|bleier)\b"),), (), 100),
    _IdentityFamily("nicotine", (), (("unspecified", r"\b(?:nicotine|tobacco|snus)\b"),), (), 100),
    _IdentityFamily(
        "produce-box", (), (("unspecified", r"\b(?:fruit\s+(?:box|basket)|fruktkasse)\b"),), (), 100
    ),
    _IdentityFamily("tonic", (), (("unspecified", r"\b(?:tonic|tonic water)\b"),), (), 100),
    _IdentityFamily(
        "licorice", (), (("unspecified", r"\b(?:lakris|licorice|liquorice)\b"),), (), 100
    ),
    _IdentityFamily(
        "peanut-butter", (), (("unspecified", r"\b(?:peanut butter|peanøttsmør)\b"),), (), 100
    ),
    _IdentityFamily(
        "vanilla-ice-cream",
        (),
        (("unspecified", r"\b(?:vanilla ice\s?cream|vaniljeis|gelato|softis)\b"),),
        (),
        100,
    ),
    _IdentityFamily(
        "coffee-creamer",
        (),
        (("unspecified", r"\b(?:coffee mate|kaffefløte|kaffekrem|kaffemelk)\b"),),
        (),
        100,
    ),
    _IdentityFamily(
        "coffee-whitener",
        (),
        (("unspecified", r"\bcoffee[\s-]+whiteners?\b"),),
        (),
        100,
    ),
    _IdentityFamily(
        "rice-drink",
        (),
        (("unspecified", r"\b(?:rice\s+milk|rice\s+drink|rismelk|risdrikk)\b"),),
        (),
        100,
    ),
    _IdentityFamily("pasta", (), (("unspecified", r"\b(?:pasta|spaghetti|linguine)\b"),), (), 80),
    _IdentityFamily(
        "flour-mix",
        (),
        (("unspecified", r"\b(?:pancake mix|cake mix|pannekakeblanding|kakemiks|baking mix)\b"),),
        (),
        100,
    ),
)
_QUERY_IDENTITIES = {
    alias: (family, variant, localized)
    for family in _IDENTITY_FAMILIES
    for alias, variant, localized in family.queries
}
_QUERY_IDENTITY_PATTERN = re.compile(
    r"(?<!\w)("
    + "|".join(re.escape(alias) for alias in sorted(_QUERY_IDENTITIES, key=len, reverse=True))
    + r")(?!\w)",
    re.I,
)
_PRODUCT_IDENTITIES = tuple(
    (family, variant, re.compile(pattern, re.I))
    for family in sorted(_IDENTITY_FAMILIES, key=lambda item: item.priority, reverse=True)
    for variant, pattern in family.products
)
_EXPLICIT_MANUAL_INTENT = re.compile(
    r"\b(?:shower\s+(?:gel|oil)|dusjgel[eé]|body\s+wash|dish\s+soap|coffee\s+mate|peanut\s+butter|peanøttsmør)\b",
    re.I,
)
_ALLOWED_QUERY_QUALIFIERS = frozenset(
    {
        "chopped",
        "diced",
        "minced",
        "hakket",
        "terninger",
        "finhakket",
        "for",
        "dusting",
        "plus",
        "extra",
        "greasing",
        "separated",
        "to",
        "taste",
        "as",
        "needed",
    }
)


def _clean_ingredient_query(query: str) -> str:
    cleaned = " ".join(query.split())
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = _RECIPE_INSTRUCTION_SUFFIX.sub("", cleaned).strip(" ,")
    return cleaned


def _query_identity(
    query: str,
) -> tuple[_IdentityFamily, str | None, str | None] | None:
    cleaned = _clean_ingredient_query(query)
    if _EXPLICIT_MANUAL_INTENT.search(cleaned):
        return None
    match = _QUERY_IDENTITY_PATTERN.search(cleaned)
    if match is None:
        return None
    residue = cleaned[: match.start()] + " " + cleaned[match.end() :]
    residue_words = re.sub(r"[,;]+", " ", residue).split()
    if any(word.casefold() not in _ALLOWED_QUERY_QUALIFIERS for word in residue_words):
        return None
    return _QUERY_IDENTITIES[match.group(0).casefold()]


def _product_identity(product: ProviderProduct) -> tuple[_IdentityFamily, str] | None:
    text = " ".join((product.name, product.brand or "")).casefold()
    for family, variant, pattern in _PRODUCT_IDENTITIES:
        if pattern.search(text):
            return family, variant
    return None


def _identity_status(
    query: str, product: ProviderProduct
) -> Literal["compatible", "unverified"] | None:
    query_identity = _query_identity(query)
    if query_identity is None:
        return "unverified"
    query_family, requested_variant, _ = query_identity
    found = _product_identity(product)
    if found is None:
        return "unverified"
    product_family, product_variant = found
    if product_family.name != query_family.name:
        return None
    if requested_variant is None:
        return "compatible" if product_variant in query_family.generic_variants else "unverified"
    if product_variant == requested_variant:
        return "compatible"
    if product_variant == "unspecified":
        return "unverified"
    return None


def _product_matches_ingredient(query: str, product: ProviderProduct) -> bool:
    return _identity_status(query, product) == "compatible"


def _bokmal_ingredient_query(query: str) -> str | None:
    """Translate known food terms and common form qualifiers for Oda search."""
    if _EXPLICIT_MANUAL_INTENT.search(query) or re.search(r"\bcoffee[\s-]*mate\b", query, re.I):
        return None
    cleaned = _clean_ingredient_query(query)
    identity_match = _QUERY_IDENTITY_PATTERN.search(cleaned)
    identity = _query_identity(cleaned)
    if identity_match is not None and identity is None:
        return None
    if identity_match is not None and identity is not None:
        localized = identity[2]
        if localized:
            translated = (
                cleaned[: identity_match.start()] + localized + cleaned[identity_match.end() :]
            )
            translated = _QUALIFIER_WORDS.sub(
                lambda match: _BOKMAL_QUALIFIERS[match.group(0).casefold()], translated
            )
            translated = " ".join(translated.split())
            trailing_qualifier = _TRAILING_FORM_QUALIFIER.fullmatch(translated)
            if trailing_qualifier:
                translated = f"{trailing_qualifier.group(2)} {trailing_qualifier.group(1)}"
            return translated
    translated, count = _INGREDIENT_PHRASES.subn(
        lambda match: _LEGACY_SEARCH_EXPANSIONS[match.group(0).casefold()], cleaned
    )
    if not count:
        return None
    translated = _QUALIFIER_WORDS.sub(
        lambda match: _BOKMAL_QUALIFIERS[match.group(0).casefold()], translated
    )
    translated = " ".join(translated.split())
    trailing_qualifier = _TRAILING_FORM_QUALIFIER.fullmatch(translated)
    if trailing_qualifier:
        translated = f"{trailing_qualifier.group(2)} {trailing_qualifier.group(1)}"
    return translated


def _query_expansions(query: str) -> tuple[str, ...]:
    localized = _bokmal_ingredient_query(query)
    if localized and localized.casefold() != query.casefold():
        return query, localized
    return (query,)


class BasketCandidate(BaseModel):
    """A provider result with explicit unknowns kept unknown."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    product_id: str
    name: str
    description: str
    brand: str | None = None
    product_url: HttpUrl | None = None
    price: str | None = None
    unit_price: str | None = None
    unit_name: str | None = None
    available: bool | None = None
    media: ProductMedia | None = None
    pack_description: str | None = None
    pack_quantity: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    pack_unit: str | None = None
    pack_metadata_source: Literal["product_page", "oda_description"] | None = None
    pack_metadata_status: Literal["unknown", "unverified"] = "unknown"
    match_query: str
    match_reason: str
    identity_status: Literal["compatible", "unverified"] = "unverified"
    recommended_packages: int | None = Field(default=None, gt=0)
    recommended_fulfillment_quantity: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )


class ProviderProduct(BaseModel):
    """Provider-neutral fields needed by matching and the review UI."""

    provider: str
    product_id: str
    name: str
    description: str
    brand: str | None = None
    product_url: HttpUrl | None = None
    price: str | None = None
    unit_price: str | None = None
    unit_name: str | None = None
    available: bool | None = None
    identity_status: Literal["compatible", "unverified"] = "unverified"


class ProviderSearchGroup(BaseModel):
    query: str
    has_more: bool
    products: list[ProviderProduct]


class NeedMatchResult(BaseModel):
    need_id: UUID
    name: str
    quantity: Decimal
    unit: str
    page: int
    has_more: bool
    candidates: list[BasketCandidate]
    status: str = "matched"
    detail: str | None = None
    approximate: bool = False
    provenance: str | None = None
    source_url: HttpUrl | None = None


class BasketMatchResult(BaseModel):
    start: date
    end: date
    needs: list[NeedMatchResult]
    skipped_need_count: int = 0


class SearchProvider(Protocol):
    async def search_products(
        self, actor_id: str, queries: list[str], page: int, size: int
    ) -> list[ProviderSearchGroup]:
        """Search a bounded batch and return normalized provider candidates."""


class OdaSearchAdapter:
    """Normalize the official Oda response to the provider-neutral protocol."""

    # The official endpoint accepts ten queries; reserve room for one localized
    # expansion per need while keeping the provider request bounded.
    max_queries = ODA_QUERY_BATCH_SIZE

    def __init__(self, service: OdaService):
        self.service = service

    async def cache_context(self, actor_id: str) -> Hashable | None:
        """Read connection lifecycle state without blocking the event loop."""
        context = getattr(self.service, "search_cache_context", None)
        return await run_in_threadpool(context, actor_id) if context is not None else None

    async def search_products(
        self, actor_id: str, queries: list[str], page: int, size: int
    ) -> list[ProviderSearchGroup]:
        expanded_by_query = {query: _query_expansions(query) for query in queries}
        provider_queries = list(
            dict.fromkeys(expanded for query in queries for expanded in expanded_by_query[query])
        )
        response = OdaSearchResponse.model_validate(
            await self.service.search_products(actor_id, provider_queries, page, size)
        )
        groups = {}
        for group in response.result:
            groups[" ".join(group.query.casefold().split())] = (
                group.has_more,
                [
                    ProviderProduct(
                        provider="oda",
                        product_id=str(product.id),
                        name=product.name,
                        description=product.description,
                        brand=product.brand,
                        product_url=product.url,
                        price=product.price,
                        unit_price=product.unit_price,
                        unit_name=product.unit_name,
                        available=(
                            product.availability.is_available if product.availability else None
                        ),
                    )
                    for product in group.products
                ],
            )

        result = []
        for query in queries:
            product_map = OrderedDict()
            has_more = False
            for expanded in expanded_by_query[query]:
                group = groups.get(" ".join(expanded.casefold().split()))
                if group is None:
                    continue
                group_has_more, group_products = group
                has_more = has_more or group_has_more
                for product in group_products:
                    identity_status = _identity_status(query, product)
                    if identity_status is not None:
                        product_map.setdefault(
                            (product.provider, product.product_id),
                            product.model_copy(update={"identity_status": identity_status}),
                        )
            result.append(
                ProviderSearchGroup(
                    query=query,
                    has_more=has_more,
                    products=list(product_map.values()),
                )
            )
        return result


MediaFetcher = Callable[[str], Awaitable[ProductMedia]]


class _MediaCache:
    def __init__(
        self, *, ttl: timedelta = MEDIA_CACHE_TTL, max_entries: int = MEDIA_CACHE_MAX_ENTRIES
    ):
        self.ttl = ttl
        self.max_entries = max_entries
        self._values: OrderedDict[str, tuple[datetime, ProductMedia]] = OrderedDict()

    def get(self, url: str, now: datetime) -> ProductMedia | None:
        entry = self._values.get(url)
        if entry is None:
            return None
        expires_at, media = entry
        if expires_at <= now:
            del self._values[url]
            return None
        self._values.move_to_end(url)
        return media

    def put(self, url: str, media: ProductMedia, now: datetime) -> None:
        self._values[url] = (now + self.ttl, media)
        self._values.move_to_end(url)
        while len(self._values) > self.max_entries:
            self._values.popitem(last=False)


_PACK_MEASURE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|cl|ml|stk|pcs?)$", re.I)
_MULTIPACK = re.compile(r"^(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|cl|ml|stk|pcs?)$", re.I)


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", "."))


def _explicit_pack(value: str | None) -> tuple[Decimal, str] | None:
    """Parse only a complete simple or explicit multipack measurement."""
    if not value or len(value) > 64:
        return None
    text = " ".join(value.split())
    match = _PACK_MEASURE.fullmatch(text)
    if match is None:
        match = _MULTIPACK.fullmatch(text)
        if match is None:
            return None
        count = int(match.group(1))
        if not 1 <= count <= 999:
            return None
        quantity = multiply_integer_exact(_decimal(match.group(2)), count)
        unit = match.group(3)
    else:
        quantity = _decimal(match.group(1))
        unit = match.group(2)
    unit = normalize_unit(unit)
    if unit in {"stk", "pc", "pcs"}:
        unit = "item"
    quantity, unit = canonicalize_quantity(quantity, unit)
    if not quantity.is_finite() or quantity <= 0:
        return None
    return quantity, unit


def _oda_description_pack(value: str) -> tuple[Decimal, str] | None:
    """Accept a complete Oda pack descriptor, never a measurement inside prose."""
    if len(value) > 256:
        return None
    pack = _explicit_pack(value)
    if pack is not None:
        return pack
    # Only a comma followed by whitespace separates descriptors. A decimal
    # comma (including the one in "0,5% fett") must remain part of its number.
    descriptors = re.split(r",\s+", value.strip())
    if len(descriptors) < 2:
        return None
    prefix = ", ".join(descriptors[:-1])
    # Percentages describe milk fat, etc. Other numbers may be competing pack
    # sizes, ranges, or nutrition values and are deliberately left unresolved.
    prefix_without_percent = re.sub(r"\d+(?:[.,]\d+)?\s*%", "", prefix)
    if (
        re.search(r"\d|[/×=<>]", prefix_without_percent)
        or not all(part.strip() for part in descriptors)
        or re.search(
            r"\b(per|pr|ca|cirka|omtrent|fra|til|næringsinnhold|nutrition|"
            r"energi|energy|protein|karbohydrat|carbohydrate|salt|kcal|kj)\b",
            prefix,
            re.I,
        )
    ):
        return None
    return _explicit_pack(descriptors[-1])


def _package_recommendation(
    need_quantity: Decimal, need_unit: str, pack: tuple[Decimal, str] | None
) -> tuple[int, Decimal] | None:
    if pack is None:
        return None
    pack_quantity, pack_unit = pack
    need_quantity, need_unit = canonicalize_quantity(need_quantity, need_unit)
    if need_unit == "pack" and pack_quantity > 0:
        packages = int(need_quantity.to_integral_value(rounding=ROUND_CEILING))
        if packages > 99:
            return None
        return packages, Decimal(packages)
    if need_unit not in compatible_units(pack_unit):
        return None
    if need_unit != pack_unit:
        pack_quantity, pack_unit = canonicalize_quantity(pack_quantity, pack_unit)
        need_quantity, need_unit = canonicalize_quantity(need_quantity, need_unit)
    if pack_unit != need_unit or pack_quantity <= 0:
        return None
    with localcontext() as context:
        context.prec = max(
            28, len(need_quantity.as_tuple().digits) + len(pack_quantity.as_tuple().digits) + 8
        )
        packages = int((need_quantity / pack_quantity).to_integral_value(rounding=ROUND_CEILING))
        if packages > 99:
            return None
        fulfillment = multiply_integer_exact(pack_quantity, packages)
    return packages, fulfillment


class BasketMatchingService:
    """Find candidates for current positive inventory-adjusted purchase needs."""

    def __init__(
        self,
        inventory: InventoryService,
        provider: SearchProvider,
        *,
        media_fetcher: MediaFetcher = fetch_product_media,
        media_cache: _MediaCache | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.inventory = inventory
        self.provider = provider
        self.media_fetcher = media_fetcher
        self.media_cache = media_cache or _MediaCache()
        self.clock = clock
        self._search_cache: OrderedDict[tuple, tuple[datetime, ProviderSearchGroup]] = OrderedDict()
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._context_reads: dict[str, asyncio.Task] = {}
        self._search_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
        self._search_tasks: set[asyncio.Task] = set()
        self._media_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MEDIA)

    async def _context(self, actor_id: str) -> Hashable | None:
        context = getattr(self.provider, "cache_context", None)
        if context is None:
            return id(self.provider)
        # A cache hit for a whole plan needs one shared lifecycle read, rather
        # than a database read for every need resolving on the same loop turn.
        task = self._context_reads.get(actor_id)
        if task is None:
            task = asyncio.create_task(context(actor_id))
            self._context_reads[actor_id] = task

            def clear(completed):
                if self._context_reads.get(actor_id) is completed:
                    del self._context_reads[actor_id]

            task.add_done_callback(clear)
        return await asyncio.shield(task)

    async def _search(
        self,
        actor_id: str,
        queries: list[str],
        page: int,
        size: int,
        force_refresh: bool,
        on_result: Callable[[str, ProviderSearchGroup | Exception | None], Awaitable[None]],
    ) -> None:
        context = await self._context(actor_id)
        pending = {}
        owned = []
        for query in queries:
            normalized = " ".join(query.casefold().split())
            if normalized in pending:
                continue
            key = (actor_id, id(self.provider), context, normalized, page, size)
            cached = self._search_cache.get(key) if context is not None else None
            if cached is not None and cached[0] > self.clock() and not force_refresh:
                self._search_cache.move_to_end(key)
                future = asyncio.get_running_loop().create_future()
                future.set_result(cached[1])
            elif context is not None and key in self._inflight:
                future = self._inflight[key]
            else:
                self._search_cache.pop(key, None)
                future = asyncio.get_running_loop().create_future()
                if context is not None:
                    self._inflight[key] = future
                owned.append((key, query, future))
            pending[normalized] = future

        async def fetch(batch):
            try:
                async with self._search_semaphore:
                    payload = await asyncio.wait_for(
                        self.provider.search_products(
                            actor_id, [query for _, query, _ in batch], page, size
                        ),
                        timeout=SEARCH_TIMEOUT_SECONDS,
                    )
                unchanged = context is not None and await self._context(actor_id) == context
                groups = {" ".join(group.query.casefold().split()): group for group in payload}
                for key, _, future in batch:
                    group = groups.get(key[3])
                    if context is not None and not unchanged:
                        group = RuntimeError("Provider connection changed during search")
                    if unchanged and isinstance(group, ProviderSearchGroup):
                        ttl = SEARCH_CACHE_TTL if group.products else SEARCH_EMPTY_CACHE_TTL
                        self._search_cache[key] = (self.clock() + ttl, group)
                        self._search_cache.move_to_end(key)
                        while len(self._search_cache) > SEARCH_CACHE_MAX_ENTRIES:
                            self._search_cache.popitem(last=False)
                    future.set_result(group)
            except BaseException as error:
                for _, _, future in batch:
                    if not future.done():
                        future.set_result(
                            error
                            if isinstance(error, Exception)
                            else RuntimeError("Search cancelled")
                        )
                if not isinstance(error, Exception):
                    raise
            finally:
                for key, _, future in batch:
                    if self._inflight.get(key) is future:
                        del self._inflight[key]

        batch_size = max(1, min(10, int(getattr(self.provider, "max_queries", 10))))
        for offset in range(0, len(owned), batch_size):
            task = asyncio.create_task(fetch(owned[offset : offset + batch_size]))
            self._search_tasks.add(task)
            task.add_done_callback(self._search_tasks.discard)

        async def resolve(query, future):
            group = await asyncio.shield(future)
            if context is not None and await self._context(actor_id) != context:
                group = RuntimeError("Provider connection changed during search")
            await on_result(query, group)

        await asyncio.gather(*(resolve(query, future) for query, future in pending.items()))

    async def find_products(
        self,
        actor_id: str,
        start: date,
        end: date,
        *,
        page: int = 1,
        size: int = 5,
        force_refresh: bool = False,
        progress: Callable[[NeedMatchResult], Awaitable[None]] | None = None,
        plan: PurchasePlan | None = None,
    ) -> BasketMatchResult:
        if end < start:
            raise ValueError("end date must be on or after start date")
        if not 1 <= page <= 50 or not 1 <= size <= MAX_RESULTS_PER_NEED:
            raise ValueError("page or result size is outside the supported range")
        if plan is None:
            plan = await run_in_threadpool(self.inventory.plan, start, end)
        purchase_lines = plan.purchase_lines
        bounded_lines = purchase_lines[:MAX_NEEDS]
        skipped_count = max(0, len(purchase_lines) - len(bounded_lines))
        results = [
            NeedMatchResult(
                need_id=line.id,
                name=line.name,
                quantity=line.to_buy_quantity,
                unit=line.unit,
                page=page,
                has_more=False,
                candidates=[],
                status="searching",
                approximate=line.approximate,
                provenance=line.provenance,
                source_url=line.source_url,
            )
            for line in bounded_lines
        ]
        if not bounded_lines:
            return BasketMatchResult(start=start, end=end, needs=[], skipped_need_count=0)

        media_slots = MAX_MEDIA_FETCHES

        async def enrich(candidate: BasketCandidate, current_index: int) -> BasketCandidate:
            nonlocal media_slots
            media = None
            url = str(candidate.product_url) if candidate.product_url else None
            if url:
                media = self.media_cache.get(url, self.clock())
            if url and media is None and media_slots > 0:
                media_slots -= 1
                try:
                    async with self._media_semaphore:
                        media = self.media_cache.get(url, self.clock())
                        if media is None:
                            media = await asyncio.wait_for(
                                self.media_fetcher(url), timeout=MEDIA_TIMEOUT_SECONDS
                            )
                            self.media_cache.put(url, media, self.clock())
                except Exception:
                    pass
            pack_description = media.pack_description if media else None
            pack = _explicit_pack(pack_description)
            pack_source = "product_page" if pack else None
            # A valid explicit page pack wins. The official search description
            # is only a fallback; names, titles and unit prices are never used.
            if pack is None and candidate.provider == "oda":
                pack = _oda_description_pack(candidate.description)
                if pack:
                    pack_description = candidate.description
                    pack_source = "oda_description"
            recommendation = _package_recommendation(
                bounded_lines[current_index].to_buy_quantity,
                bounded_lines[current_index].unit,
                pack,
            )
            return candidate.model_copy(
                update={
                    "media": media,
                    "pack_description": pack_description,
                    "pack_quantity": pack[0] if pack else None,
                    "pack_unit": pack[1] if pack else None,
                    "pack_metadata_source": pack_source,
                    "pack_metadata_status": "unverified" if pack_description else "unknown",
                    "recommended_packages": recommendation[0] if recommendation else None,
                    "recommended_fulfillment_quantity": recommendation[1]
                    if recommendation
                    else None,
                }
            )

        async def match_need(current_index: int, outcome):
            if isinstance(outcome, BaseException):
                results[current_index].status = "search_failed"
                results[current_index].detail = "Product search failed or timed out."
                if progress is not None:
                    await progress(results[current_index])
                return
            try:
                group = outcome
                if group is None:
                    results[current_index].status = "search_unavailable"
                    results[
                        current_index
                    ].detail = "The provider returned no result group for this query."
                    return
                candidates = [
                    BasketCandidate(
                        id=f"{product.provider}:{product.product_id}",
                        provider=product.provider,
                        product_id=product.product_id,
                        name=product.name,
                        description=product.description,
                        brand=product.brand,
                        product_url=product.product_url,
                        price=product.price,
                        unit_price=product.unit_price,
                        unit_name=product.unit_name,
                        available=product.available,
                        match_query=group.query,
                        match_reason=(
                            f"{'Identity compatible' if product.identity_status == 'compatible' else 'Identity unverified; explicit confirmation required'}; "
                            f"returned by {product.provider} product search for the need name "
                            f"'{group.query}'."
                        ),
                        identity_status=product.identity_status,
                    )
                    for product in group.products[:size]
                ]
                results[current_index].has_more = group.has_more or len(group.products) > size
                results[current_index].candidates = await asyncio.gather(
                    *(enrich(candidate, current_index) for candidate in candidates)
                )
                results[current_index].status = "matched" if candidates else "no_results"
                if group.has_more:
                    results[
                        current_index
                    ].detail = "More provider results are available on the next page."
            except Exception:
                results[current_index].status = "search_unavailable"
                results[current_index].detail = "The provider response could not be interpreted."
            finally:
                if progress is not None:
                    await progress(results[current_index])

        async def on_result(query, outcome):
            await asyncio.gather(
                *(
                    match_need(index, outcome)
                    for index, line in enumerate(bounded_lines)
                    if " ".join(line.name.casefold().split()) == query
                )
            )

        try:
            await self._search(
                actor_id,
                [line.name for line in bounded_lines],
                page,
                size,
                force_refresh,
                on_result,
            )
        except Exception as error:
            await asyncio.gather(
                *(
                    match_need(index, error)
                    for index, result in enumerate(results)
                    if result.status == "searching"
                )
            )

        return BasketMatchResult(
            start=start,
            end=end,
            needs=results,
            skipped_need_count=skipped_count,
        )
