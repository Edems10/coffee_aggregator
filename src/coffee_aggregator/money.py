from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

GRAMS_PER_KG = 1000.0
#: Money is stored to the cent (and to the heller), never to the float's last bit.
CENTS = Decimal("0.01")


def exact(amount: float) -> Decimal | None:
    """Turn a shop's float price into an exact decimal.

    Going through ``str`` is deliberate: ``Decimal(19.9)`` keeps the binary
    float's tail, ``Decimal("19.9")`` is the price the shop printed.

    Args:
        amount: The price as parsed from the page.

    Returns:
        The exact decimal, or None for a NaN or an infinity.
    """
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def round_cents(value: Decimal) -> float:
    """Round an amount to two decimals, half away from zero.

    Args:
        value: The exact amount.

    Returns:
        The rounded amount as a float, ready for a ``numeric`` column.
    """
    return float(value.quantize(CENTS, rounding=ROUND_HALF_UP))


def per_kg(amount: float | None, weight_g: int | None) -> float | None:
    """Extrapolate an amount for one package to one kilogram.

    Args:
        amount: The price of one package, in any currency.
        weight_g: The net weight of that same package.

    Returns:
        The price of a kilogram rounded to the cent, or None when either input
        is missing or the weight is zero.
    """
    if amount is None or not weight_g:
        return None
    value = exact(amount)
    if value is None:
        return None
    try:
        scaled = value * Decimal(str(GRAMS_PER_KG)) / Decimal(weight_g)
    except InvalidOperation:
        return None
    if not scaled.is_finite():
        return None
    return round_cents(scaled)
