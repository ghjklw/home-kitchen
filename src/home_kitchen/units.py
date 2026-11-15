"""Exact, deliberately small mass and volume unit conversions."""

from decimal import Decimal, localcontext

# Number of base-unit decimal places to shift: grams for mass, millilitres for
# volume. Keeping this list explicit prevents guesses about density or aliases.
_UNIT_SCALE: dict[str, tuple[str, int]] = {
    "g": ("g", 0),
    "kg": ("g", 3),
    "ml": ("ml", 0),
    "cl": ("ml", 1),
    "dl": ("ml", 2),
    "l": ("ml", 3),
}


def normalize_unit(unit: str) -> str:
    """Collapse case and whitespace without interpreting unknown unit labels."""
    return " ".join(unit.split()).casefold()


def canonical_unit(unit: str) -> str:
    normalized = normalize_unit(unit)
    mapping = _UNIT_SCALE.get(normalized)
    return mapping[0] if mapping else normalized


def canonicalize_quantity(quantity: Decimal, unit: str) -> tuple[Decimal, str]:
    """Convert supported units to g/ml using a context-independent decimal shift."""
    normalized = normalize_unit(unit)
    mapping = _UNIT_SCALE.get(normalized)
    if mapping is None:
        return quantity, normalized

    canonical, exponent_shift = mapping
    sign, digits, exponent = quantity.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("quantity must be finite")
    return Decimal((sign, digits, exponent + exponent_shift)), canonical


def add_exact(left: Decimal, right: Decimal) -> Decimal:
    """Add finite decimals without rounding at the process-wide context precision."""
    left_tuple = left.as_tuple()
    right_tuple = right.as_tuple()
    if not isinstance(left_tuple.exponent, int) or not isinstance(right_tuple.exponent, int):
        raise ValueError("quantities must be finite")
    exponent = min(left_tuple.exponent, right_tuple.exponent)
    precision = (
        max(
            len(left_tuple.digits) + left_tuple.exponent - exponent,
            len(right_tuple.digits) + right_tuple.exponent - exponent,
        )
        + 1
    )
    with localcontext() as context:
        context.prec = max(1, precision)
        return left + right


def multiply_integer_exact(value: Decimal, multiplier: int) -> Decimal:
    """Multiply by package counts without Decimal context rounding."""
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("quantity must be finite")
    coefficient = int("".join(str(digit) for digit in digits) or "0") * abs(multiplier)
    result_digits = tuple(int(digit) for digit in str(coefficient))
    result_sign = sign if multiplier >= 0 else 1 - sign
    return Decimal((result_sign, result_digits, exponent))


def compatible_units(unit: str) -> frozenset[str]:
    """Return persisted unit keys that share this explicitly supported family."""
    normalized = normalize_unit(unit)
    mapping = _UNIT_SCALE.get(normalized)
    if mapping is None:
        return frozenset({normalized})
    canonical = mapping[0]
    return frozenset(key for key, (base, _) in _UNIT_SCALE.items() if base == canonical)
