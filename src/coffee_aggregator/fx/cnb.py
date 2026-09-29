from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from coffee_aggregator.fx.rates import BASE_CURRENCY, FxRate

if TYPE_CHECKING:
    from coffee_aggregator.http import PoliteFetcher

logger = logging.getLogger(__name__)

#: The Czech National Bank's own JSON service. It answers with the newest fixing
#: on or before the date asked for, so a weekend, a holiday or a run before the
#: afternoon publication all return the last working day by themselves.
CNB_URL: Final = "https://api.cnb.cz/cnbapi/exrates/daily"
SOURCE: Final = "cnb"

_DATE_FORMAT: Final = "%Y-%m-%d"
_CODE_LENGTH: Final = 3


def url_for(day: date | None = None) -> str:
    """Build the request URL for one day.

    Args:
        day: The day wanted; today when omitted.

    Returns:
        The absolute URL, always asking for the English payload so the country
        and currency names cannot change under a Czech locale.
    """
    asked = day or datetime.now(tz=None).date()  # noqa: DTZ005  (a bank day, not an instant)
    return f"{CNB_URL}?date={asked.isoformat()}&lang=EN"


def parse_rate(text: str, code: str = BASE_CURRENCY) -> FxRate | None:
    """Pull one currency's rate out of the bank's JSON answer.

    Args:
        text: The response body.
        code: The ISO code wanted; the project only ever asks for EUR.

    Returns:
        The rate, or None when the answer is unusable or lists no such currency.
    """
    rows = _rows(text)
    if rows is None:
        return None
    wanted = code.upper()
    for row in rows:
        if str(row.get("currencyCode", "")).upper() != wanted:
            continue
        rate = _rate_of(row)
        day = _day_of(row)
        if rate is None or day is None:
            return None
        return FxRate(date=day, rate=rate, source=SOURCE, base=wanted)
    logger.warning("the CNB answer lists no %s row", wanted)
    return None


def _rows(text: str) -> list[dict[str, Any]] | None:
    """Read the ``rates`` array out of the response.

    Args:
        text: The response body.

    Returns:
        The rows, or None when the body is not the JSON the bank documents.
    """
    try:
        # Money never goes through a float: parse_float keeps the wire's own digits.
        payload = json.loads(text, parse_float=Decimal)
    except ValueError:
        logger.warning("the CNB answer is not JSON")
        return None
    rows = payload.get("rates") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        logger.warning("the CNB answer carries no rates")
        return None
    return [row for row in rows if isinstance(row, dict)]


def _rate_of(row: dict[str, Any]) -> Decimal | None:
    """Read one row's rate per single unit of the currency.

    The bank quotes some currencies per hundred, which ``amount`` states, so the
    rate is only comparable once divided by it.

    Args:
        row: One entry of the ``rates`` array.

    Returns:
        The rate for one unit, or None when the row does not state a usable one.
    """
    try:
        rate = Decimal(str(row["rate"]))
        amount = Decimal(str(row.get("amount", 1)))
    except KeyError, TypeError, ValueError, InvalidOperation:
        return None
    if not amount or rate <= 0:
        return None
    return rate / amount


def _day_of(row: dict[str, Any]) -> date | None:
    """Read the day a row is the fixing for.

    Args:
        row: One entry of the ``rates`` array.

    Returns:
        The day, or None when it is missing or malformed.
    """
    stamp = str(row.get("validFor", "")).strip()
    try:
        return datetime.strptime(stamp, _DATE_FORMAT).replace(tzinfo=None).date()  # noqa: DTZ007
    except ValueError:
        logger.warning("the CNB answer states an unreadable date %r", stamp)
        return None


def fetch_rate(fetcher: PoliteFetcher, day: date | None = None) -> FxRate | None:
    """Ask the bank for the fixing in force on one day.

    Args:
        fetcher: The project's polite fetcher; robots.txt and the per-host delay
            apply to the bank exactly as they do to a shop.
        day: The day wanted; today when omitted.

    Returns:
        The EUR/CZK rate, or None when the answer could not be read.
    """
    return parse_rate(fetcher.get(url_for(day)).text)
