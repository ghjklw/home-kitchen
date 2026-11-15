"""Conservative preservation of explicit source measures, without unit conversion."""

import re
from decimal import Decimal

# These are source labels, including opaque counts. They make no claim about mass,
# volume or purchasing equivalence (a handful cannot be converted to grams here).
_UNITS = (
    "håndfuller",
    "håndfull",
    "klyper",
    "klype",
    "stk",
    "kg",
    "mg",
    "ml",
    "cl",
    "dl",
    "ts",
    "ss",
    "g",
    "l",
)
_MEASURE = re.compile(
    r"^\s*(?P<quantity>[0-9]+(?:[.,][0-9]+)?)\s*"
    r"(?P<unit>" + "|".join(_UNITS) + r")(?=\s|$|[A-ZÆØÅ])",
)


def explicit_source_measure(text: str) -> tuple[Decimal, str] | None:
    """Read a positive numeric prefix and allowlisted label; never infer a measure.

    A capitalized ingredient may immediately follow a unit in scraped source text
    (``1klypeSalt``). Lowercase continuations are rejected to avoid reading ``1 laks``
    as one litre. Ranges, fractions, qualitative amounts and unknown units stay unknown.
    """
    match = _MEASURE.match(text)
    if match is None:
        return None
    quantity = Decimal(match["quantity"].replace(",", "."))
    if quantity <= 0:
        return None
    return quantity, match["unit"]
