"""Domain logic and models for read-only product matching in the basket review workflow.

Provider results stay as candidates until a person selects one through the
existing basket review flow. Product-page pack text is treated as unverified
metadata and only a complete, unambiguous measurement can support a quantity.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from home_kitchen.catalogue.identity import IdentityCorrectionCitation
from home_kitchen.oda.media import ProductMedia
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
SEARCH_CACHE_VERSION = 3

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
    "baking powder": "bakepulver",
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
    "hvetemel": "hvetemel",
    "usaltet smør": "usaltet smør",
    "helmelk": "helmelk",
    "lettmelk": "lettmelk",
    "sukker": "sukker",
    "ris": "ris",
    "basmatiris": "basmatiris",
    "olivenolje": "olivenolje",
    "bakepulver": "bakepulver",
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
    r"for dusting|plus extra for greasing|separated|to taste|as needed|"
    r"at room temperature|room temperature|melted|softened|divided|beaten)$",
    re.I,
)
_RECIPE_INSTRUCTION_SUFFIX = re.compile(
    r"(?:,\s*|\s+)(?:plus\s+extra\s+for\s+(?:greasing|dusting|brushing|frying|serving)|"
    r"for\s+(?:greasing|dusting|brushing|frying|serving)(?:\s+(?:the\s+)?(?:pan|tin|dish|mold|mould|bowl))?|"
    r"separated|to\s+taste|as\s+needed|at\s+room\s+temperature|room\s+temperature|"
    r"melted\s+and\s+cooled|softened\s+and\s+cubed|melted|softened|divided|"
    r"(?:lightly|well)\s+beaten|beaten|sifted|chilled|cubed|cut\s+into\s+(?:cubes|pieces))\s*$",
    re.I,
)


@dataclass(frozen=True)
class _IdentityFamily:
    name: str
    queries: tuple[tuple[str, str | None, str | None], ...]
    products: tuple[tuple[str, str], ...]
    generic_variants: tuple[str, ...] = ()
    priority: int = 0


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
            (
                "other",
                r"\b(?:rugmel|rye flour|speltmel|spelt flour|maismel|corn\s*flour|cornmeal|"
                r"potetmel|potato\s*(?:flour|starch)|kokosmel|coconut flour|rismel|rice flour|"
                r"kikertmel|chickpea flour|bokhvetemel|boghvetemel|buckwheat flour|soyamel|soy flour)\b",
            ),
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
            ("egg l", "large", None),
            ("store egg", "large", None),
            ("eggs", None, "egg"),
            ("egg", None, "egg"),
        ),
        (
            (
                "large",
                r"\b(?:egg|eggs)\b.*\b(?:large|store|size\s*l|str\.?\s*l|xl|m/l|(?<!\d\s)(?<!\d)l)\b|"
                r"\b(?:large|store|xl|m/l)\b.*\b(?:egg|eggs)\b",
            ),
            (
                "medium",
                r"\b(?:egg|eggs)\b.*\b(?:medium|size\s*m|str\.?\s*m|(?<!\d\s)(?<!\d)m)\b",
            ),
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
            ("caster", r"\b(?:caster sugar|finsukker|finkornet sukker)\b"),
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
        "baking-powder",
        (
            ("baking powder", "powder", "bakepulver"),
            ("bakepulver", "powder", None),
        ),
        (("powder", r"\b(?:baking powder|bakepulver)\b"),),
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
            ("kyllingbryst", "breast", None),
            ("kyllingbrystfilet", "breast", None),
            ("kyllinglår", "thigh", None),
        ),
        (
            ("breast", r"\b(?:chicken breasts?|kylling(?:filet|bryst(?:filet)?))\b"),
            ("thigh", r"\b(?:chicken thighs?|kyllinglår)\b"),
            (
                "other",
                r"\b(?:kylling(?:kjøttdeig|vinger|buljong|postei|salat|suppe|nuggets|pølse)|"
                r"chicken\s+(?:wings?|nuggets?|stock|broth|bouillon|sausage|mince))\b",
            ),
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
    _IdentityFamily(
        "chocolate",
        (),
        (
            (
                "unspecified",
                r"\b(?:chocolate|sjokolade|melkesjokolade|kinderegg|pasukeegg|sjokoladeegg|marsipanegg)\b|\britter\s*sport\b",
            ),
        ),
        (),
        100,
    ),
    _IdentityFamily(
        "personal-care",
        (),
        (
            (
                "unspecified",
                r"\b(?:shower\s+(?:gel|oil|cream|wash)|oil\s+shower|dusj(?:gel[eé]|olje|såpe|krem)|"
                r"body\s+(?:wash|oil|butter|cream|lotion)|shea\s+butter|"
                r"baby\s*oil|babyolje|bath\s+oil|badeolje|hair\s+oil|hårolje|"
                r"massage\s+oil|massasjeolje|sun\s+oil|sololje|kroppsolje|ansiktsolje|renseolje|skjeggolje|"
                r"cleansing\s+oil|bio-oil|hand\s+cream|håndkrem|face\s+cream|ansiktskrem|hudkrem|kroppskrem|"
                r"shampoo|sjampo|conditioner|balsam|deodorant|toothpaste|tannkrem|"
                r"såpe|soap|lotion|nivea|dove|palmolive|rituals|garnier|neutrogena|vaseline|sterilan|lano)\b"
                r"|(?=.*\b(?:shower|dusj)\b)(?=.*\b(?:oil|olje|gel|wash|body|bath|care|nivea)\b)",
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
                r"\b(?:sugar\s+(?:decoration|decorations|faces)|sukkerpynt|kakepynt|dekorstrøssel|kakestrøssel|sprinkles)\b",
            ),
        ),
        (),
        100,
    ),
    _IdentityFamily("tortilla", (), (("unspecified", r"\b(?:tortillas?|wraps?)\b"),), (), 100),
    _IdentityFamily("diaper", (), (("unspecified", r"\b(?:diapers?|nappies|bleier?)\b"),), (), 100),
    _IdentityFamily(
        "nicotine",
        (),
        (
            (
                "unspecified",
                r"\b(?:nicotine|nikotin|nikotinposer|tobacco|tobakk|tobakksfri|tobaksfri|snus|"
                r"all[\s-]+white|allwhite|super[\s-]+white|helhvit|porsjonssnus|nicotine\s+pouch(?:es)?)\b|"
                r"\b(?:loop|zyn|epok|velo|skruf|lundgrens|xqs|white\s+fox)\b",
            ),
        ),
        (),
        100,
    ),
    _IdentityFamily(
        "produce-box", (), (("unspecified", r"\b(?:fruit\s+(?:box|basket)|fruktkasse)\b"),), (), 100
    ),
    _IdentityFamily(
        "tonic",
        (),
        (
            (
                "unspecified",
                r"\b(?:tonic(?:\s+water)?|farris(?:\s+bris)?|imsdal|bonaqua|raml[oö]sa|"
                r"mineralvann|kullsyrevann|sparkling\s+water|mineral\s+water|"
                r"coconut\s+water|kokosvann|micellar\s+water|rose\s+water|schweppes)\b",
            ),
        ),
        (),
        100,
    ),
    _IdentityFamily(
        "licorice", (), (("unspecified", r"\b(?:lakris|licorice|liquorice)\b"),), (), 100
    ),
    _IdentityFamily(
        "peanut-butter",
        (),
        (
            (
                "unspecified",
                r"\b(?:(?:peanut|almond|cashew|hazelnut)\s*butter|"
                r"(?:peanøtt|mandel|cashew|hasselnøtt)smør|cocoa\s*butter|kakaosmør)\b",
            ),
        ),
        (),
        100,
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
    _IdentityFamily(
        "pasta",
        (),
        (
            (
                "unspecified",
                r"\b(?:pasta|spaghetti|linguine|penne|fusilli|macaroni|makaroni|"
                r"tagliatelle|lasagne(?:plater)?|eggnudler|nudler|noodles|gnocchi)\b",
            ),
        ),
        (),
        80,
    ),
    _IdentityFamily(
        "flour-mix",
        (),
        (("unspecified", r"\b(?:pancake mix|cake mix|pannekakeblanding|kakemiks|baking mix)\b"),),
        (),
        100,
    ),
    _IdentityFamily(
        "dough",
        (),
        (
            (
                "unspecified",
                r"\b(?:[a-zæøå]*deig|cookie\s+dough|gingerbread\s+dough)\b",
            ),
        ),
        (),
        100,
    ),
    _IdentityFamily(
        "baked-goods",
        (),
        (("unspecified", r"\b(?:pepperkaker|gingerbread|kakebunn|pizzabunn)\b"),),
        (),
        100,
    ),
    _IdentityFamily(
        "cleaning",
        (),
        (
            (
                "unspecified",
                r"\b(?:vaskepulver|oppvaskpulver|washing\s+powder|detergent|allrent|rengjøring|tøyvask|skyllemiddel)\b",
            ),
        ),
        (),
        100,
    ),
    _IdentityFamily(
        "seasoning-broth",
        (),
        (
            (
                "unspecified",
                r"\b(?:seasoning|spice\s*mix|krydderblanding|marinade|buljong|bouillon|eggelikør)\b",
            ),
        ),
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
    r"\b(?:shower\s+(?:gel|oil)|dusj(?:gel[eé]|olje|såpe)|body\s+(?:wash|oil|butter)|"
    r"babyolje|hair\s+oil|shampoo|sjampo|lotion|dish\s+soap|coffee\s+mate|"
    r"peanut\s+butter|peanøttsmør)\b",
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
        "at",
        "room",
        "temperature",
        "melted",
        "softened",
        "divided",
        "beaten",
        "lightly",
        "well",
        "sifted",
        "chilled",
        "cubed",
        "cooled",
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


class StockEvidenceCitation(BaseModel):
    """Stock-offset evidence kept separate from identity and SKU preferences."""

    model_config = ConfigDict(extra="forbid")

    available_quantity: Decimal
    covered_quantity: Decimal
    unit: str


class SkuPreferenceCitation(BaseModel):
    """Household preferred/excluded SKU evidence kept separate from identity corrections."""

    model_config = ConfigDict(extra="forbid")

    preferred_product_ids: list[str] = Field(default_factory=list)
    excluded_product_ids: list[str] = Field(default_factory=list)


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
    is_preferred: bool = False
    preference_rank: int | None = Field(default=None, ge=1)
    recommended_packages: int | None = Field(default=None, gt=0)
    recommended_fulfillment_quantity: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    identity_correction_citations: list[IdentityCorrectionCitation] = Field(default_factory=list)


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
    search_query: str | None = None
    revision: int = 0
    observed_at: datetime | None = None
    identity_correction_citations: list[IdentityCorrectionCitation] = Field(default_factory=list)
    stock_evidence: StockEvidenceCitation | None = None
    sku_preference_evidence: SkuPreferenceCitation | None = None
    purchase_evidence: list[dict[str, Any]] = Field(default_factory=list)


class BasketMatchResult(BaseModel):
    start: date
    end: date
    needs: list[NeedMatchResult]
    skipped_need_count: int = 0


@dataclass(frozen=True)
class AvailabilityTarget:
    need_id: UUID
    need_name: str
    provider: str
    product_id: str
    product_name: str
    packages: int = 1
    previous_available: bool | None = None
    previous_price: str | None = None
    search_query: str | None = None


@dataclass(frozen=True)
class AvailabilityCheckOutcome:
    target: AvailabilityTarget
    available: bool | None
    availability_status: Literal["available", "unavailable", "unknown"]
    availability_description: str | None = None
    current_price: str | None = None
    price_changed: bool = False
    fresh_product: ProviderProduct | None = None
    checked_query: str | None = None


class SearchProvider(Protocol):
    async def search_products(
        self, actor_id: str, queries: list[str], page: int, size: int
    ) -> list[ProviderSearchGroup]:
        """Search a bounded batch and return normalized provider candidates."""


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
    descriptors = re.split(r",\s+", value.strip())
    if len(descriptors) < 2:
        return None
    prefix = ", ".join(descriptors[:-1])
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
