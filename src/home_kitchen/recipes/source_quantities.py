"""Parse imported recipe quantities without inventing uncertain amounts.

Ingredient-specific volume-to-mass factors are based on Matprat's Norwegian
"Mål og vekt" table: https://www.matprat.no/artikler/mengde-mal-og-vekt/mal-og-vekt/
They are estimates because ingredient packing and product varieties differ.
"Håndfull" and "klype" deliberately remain practical measures; neither has a
reliable universal mass conversion.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

MEASURE_SOURCE = "https://www.matprat.no/artikler/mengde-mal-og-vekt/mal-og-vekt/"


@dataclass(frozen=True)
class SourcePurchaseEstimate:
    """A separately derived shopping amount; never changes the recipe measure."""

    quantity: Decimal
    unit: str
    approximate: bool = False
    provenance: str = "exact metric unit conversion"
    source_url: str | None = None


@dataclass(frozen=True)
class SourceQuantity:
    """Parsed cooking measure and optional, separate purchase estimate."""

    quantity: Decimal | None
    unit: str | None
    original_text: str
    purchase_estimate: SourcePurchaseEstimate | None = None

    @property
    def approximate(self) -> bool:
        return bool(self.purchase_estimate and self.purchase_estimate.approximate)

    @property
    def provenance(self) -> str:
        return self.purchase_estimate.provenance if self.purchase_estimate else "source text"


_NUMBER = (
    r"(?:[0-9]+(?:[.,][0-9]+)?\s+[0-9]+/[0-9]+|"
    r"[0-9]+/[0-9]+|[0-9]+(?:[.,][0-9]+)?|[¼½¾⅓⅔⅛⅜⅝⅞])"
)
_UNIT_ALIASES = {
    "håndfuller": "håndfull",
    "håndfull": "håndfull",
    "handfuller": "håndfull",
    "handfull": "håndfull",
    "klyper": "klype",
    "klype": "klype",
    "ss": "ss",
    "spiseskjeer": "ss",
    "spiseskje": "ss",
    "spiseskeer": "ss",
    "spiseske": "ss",
    "ts": "ts",
    "teskjeer": "ts",
    "teskje": "ts",
    "teskeer": "ts",
    "teske": "ts",
    "stk": "stk",
    "stykker": "stk",
    "stykk": "stk",
    "kg": "kg",
    "g": "g",
    "mg": "mg",
    "ml": "ml",
    "cl": "cl",
    "dl": "dl",
    "l": "l",
}
_UNIT_PATTERN = "|".join(re.escape(unit) for unit in sorted(_UNIT_ALIASES, key=len, reverse=True))
_MEASURE = re.compile(
    rf"^\s*(?P<quantity>{_NUMBER})\s*(?P<unit>(?i:{_UNIT_PATTERN}))(?=\s|$|[A-ZÆØÅ])",
)
_TO_TASTE = re.compile(r"\better\s+smak\b", re.IGNORECASE)

# Matprat publishes grams per decilitre and grams per level tablespoon.
# Tables are ingredient-specific; no generic density or handful conversion exists.
_GRAMS_PER_DL = {
    "hvetemel": Decimal("55"),
    "hvete mel": Decimal("55"),
    "sukker": Decimal("90"),
    "melis": Decimal("55"),
    "smør": Decimal("85"),
    "smor": Decimal("85"),
    "lettkokte havregryn": Decimal("40"),
    "havregryn": Decimal("40"),
}
_GRAMS_PER_SS = {
    "hvetemel": Decimal("9"),
    "hvete mel": Decimal("9"),
    "sukker": Decimal("13"),
    "melis": Decimal("8"),
    "smør": Decimal("15"),
    "smor": Decimal("15"),
    "lettkokte havregryn": Decimal("6"),
    "havregryn": Decimal("6"),
}
_UNICODE_FRACTIONS = {
    "¼": Decimal("0.25"),
    "½": Decimal("0.5"),
    "¾": Decimal("0.75"),
    "⅓": Decimal(1) / Decimal(3),
    "⅔": Decimal(2) / Decimal(3),
    "⅛": Decimal("0.125"),
    "⅜": Decimal("0.375"),
    "⅝": Decimal("0.625"),
    "⅞": Decimal("0.875"),
}


def _number(value: str) -> Decimal | None:
    normalized = value.strip().replace(",", ".")
    if normalized in _UNICODE_FRACTIONS:
        return _UNICODE_FRACTIONS[normalized]
    if " " in normalized and "/" in normalized:
        whole, fraction = normalized.split()
        numerator, denominator = fraction.split("/", 1)
        if int(denominator) == 0:
            return None
        return Decimal(whole) + Decimal(numerator) / Decimal(denominator)
    if "/" in normalized:
        numerator, denominator = normalized.split("/", 1)
        if int(denominator) == 0:
            return None
        return Decimal(numerator) / Decimal(denominator)
    return Decimal(normalized)


def _ingredient_key(ingredient: str) -> str:
    return " ".join(ingredient.casefold().replace("æ", "ae").split())


def _ingredient_factor(ingredient: str, unit: str) -> Decimal | None:
    key = _ingredient_key(ingredient)
    # Match explicit ingredient names, including descriptive prefixes such as
    # "siktet hvetemel" and "brunt sukker", without accepting loose substrings.
    names = sorted(set(_GRAMS_PER_DL) | set(_GRAMS_PER_SS), key=len, reverse=True)
    matching = next(
        (name for name in names if key == name or key.endswith(" " + name)),
        None,
    )
    if matching is None:
        return None
    if unit == "dl":
        return _GRAMS_PER_DL.get(matching)
    per_ss = _GRAMS_PER_SS.get(matching)
    if unit == "ss":
        return per_ss
    if unit == "ts" and per_ss is not None:
        return per_ss / Decimal(3)
    return None


def normalize_source_quantity(text: str, ingredient: str = "") -> SourceQuantity:
    """Parse and normalize an imported quantity, preserving uncertain source text.

    The returned quantity and unit retain the recipe's cooking measure. An
    ingredient-specific estimate is kept separately for shopping. Unknown,
    zero, range, or qualitative amounts never receive a fabricated quantity.
    """
    original = text
    raw = text.strip()
    if _TO_TASTE.search(raw):
        return SourceQuantity(None, "etter smak", original)

    match = _MEASURE.match(raw)
    if match is None:
        return SourceQuantity(None, None, original)
    quantity = _number(match["quantity"])
    if quantity is None or quantity <= 0:
        return SourceQuantity(None, None, original)

    source_unit = _UNIT_ALIASES[match["unit"].casefold()]
    factor = _ingredient_factor(ingredient, source_unit)
    if factor is not None:
        return SourceQuantity(
            quantity,
            source_unit,
            original,
            SourcePurchaseEstimate(
                quantity=quantity * factor,
                unit="g",
                approximate=True,
                provenance=(
                    f"Matprat ingredient-specific {source_unit}-to-g table; source text: {original}"
                ),
                source_url=MEASURE_SOURCE,
            ),
        )
    return SourceQuantity(quantity, source_unit, original)


def source_ingredient_parts(text: str) -> tuple[str, SourceQuantity]:
    """Split a source line into an ingredient label and preserved measure."""
    raw = text.strip()
    match = _MEASURE.match(raw)
    if match is not None:
        ingredient = raw[match.end() :].strip(" \t,;:-")
        parsed = normalize_source_quantity(raw, ingredient)
        return ingredient or raw, parsed
    if _TO_TASTE.search(raw):
        ingredient = _TO_TASTE.sub("", raw).strip(" \t,;:-")
        parsed = normalize_source_quantity(raw, ingredient)
        return ingredient or raw, parsed
    return raw, normalize_source_quantity(raw)


def explicit_source_measure(text: str) -> tuple[Decimal, str] | None:
    """Compatibility helper for callers needing a numeric source prefix only."""
    parsed = normalize_source_quantity(text)
    if parsed.quantity is None or parsed.unit is None:
        return None
    # Keep the source's stated unit here. Call normalize_source_quantity when
    # canonical units, ingredient-specific conversions or provenance are needed.
    match = _MEASURE.match(text)
    if match is None:
        return None
    quantity = _number(match["quantity"])
    if quantity is None or quantity <= 0:
        return None
    return quantity, _UNIT_ALIASES[match["unit"].casefold()]
