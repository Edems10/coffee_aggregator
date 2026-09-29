from __future__ import annotations

from typing import TYPE_CHECKING

from coffee_aggregator.fx.rates import BASE_CURRENCY, QUOTE_CURRENCY
from coffee_aggregator.money import exact, round_cents

if TYPE_CHECKING:
    from coffee_aggregator.fx.rates import FxRate


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
    value = exact(amount)
    if value is None:
        return None
    code = currency.strip().upper()
    if code == BASE_CURRENCY:
        return round_cents(value)
    if code == QUOTE_CURRENCY and rate.rate > 0:
        return round_cents(value / rate.rate)
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
    value = exact(amount)
    if value is None:
        return None
    code = currency.strip().upper()
    if code == QUOTE_CURRENCY:
        return round_cents(value)
    if code == BASE_CURRENCY and rate.rate > 0:
        return round_cents(value * rate.rate)
    return None
