from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING

from coffee_aggregator.fx.rates import BASE_CURRENCY, QUOTE_CURRENCY

if TYPE_CHECKING:
    from coffee_aggregator.fx.rates import FxRate

#: Money is stored to the cent (and to the heller), never to the float's last bit.
_CENTS = Decimal("0.01")


def _money(amount: float) -> Decimal | None:
    """Turn a shop's float price into an exact decimal.

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


def _round(value: Decimal) -> float:
    """Round a converted amount to two decimals, half away from zero.

    Args:
        value: The exact converted amount.

    Returns:
        The rounded amount as a float, ready for a ``numeric`` column.
    """
    return float(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def to_eur(amount: float | None, currency: str | None, rate: FxRate) -> float | None:
    """Express an amount in euros.

    Args:
        amount: The price in ``currency``.
        currency: The ISO code the shop prices in.
        rate: The fixing to convert with, quoted as CZK per one EUR.

    Returns:
        The amount in EUR rounded to the cent, or None when either input is
        missing or the currency is neither EUR nor CZK.
    """
    if amount is None or not currency:
        return None
    value = _money(amount)
    if value is None:
        return None
    code = currency.strip().upper()
    if code == BASE_CURRENCY:
        return _round(value)
    if code == QUOTE_CURRENCY and rate.rate > 0:
        return _round(value / rate.rate)
    return None


def to_czk(amount: float | None, currency: str | None, rate: FxRate) -> float | None:
    """Express an amount in Czech crowns.

    Args:
        amount: The price in ``currency``.
        currency: The ISO code the shop prices in.
        rate: The fixing to convert with, quoted as CZK per one EUR.

    Returns:
        The amount in CZK rounded to the heller, or None when either input is
        missing or the currency is neither EUR nor CZK.
    """
    if amount is None or not currency:
        return None
    value = _money(amount)
    if value is None:
        return None
    code = currency.strip().upper()
    if code == QUOTE_CURRENCY:
        return _round(value)
    if code == BASE_CURRENCY and rate.rate > 0:
        return _round(value * rate.rate)
    return None
