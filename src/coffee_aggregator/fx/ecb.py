from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from coffee_aggregator.fx.rates import QUOTE_CURRENCY, FxRate

if TYPE_CHECKING:
    from coffee_aggregator.http import PoliteFetcher

logger = logging.getLogger(__name__)

#: The European Central Bank's daily reference rates — the fallback for the days
#: the Czech National Bank is unreachable. EUR is always the base here.
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
SOURCE = "ecb"

_CUBE = "Cube"
_DATE_FORMAT = "%Y-%m-%d"


def parse_rate(text: str, code: str = QUOTE_CURRENCY) -> FxRate | None:
    """Read one currency out of the ECB's daily reference file.

    The file nests three ``Cube`` elements: an outer wrapper, one carrying
    ``time``, and one per currency carrying ``currency`` and ``rate``. Every rate
    is already quoted per one euro, so nothing has to be divided.

    Args:
        text: The XML as served.
        code: The ISO code wanted; the project only ever asks for CZK.

    Returns:
        The EUR-based rate, or None when the file carried no date or no such row.
    """
    cubes = BeautifulSoup(text, "xml").find_all(_CUBE)
    day: date | None = None
    wanted = code.upper()
    for cube in cubes:
        stamp = cube.get("time")
        if isinstance(stamp, str) and day is None:
            day = _parse_date(stamp)
        if str(cube.get("currency") or "").upper() != wanted:
            continue
        rate = _decimal(str(cube.get("rate") or ""))
        if day is not None and rate is not None and rate > 0:
            return FxRate(date=day, rate=rate, source=SOURCE, quote=wanted)
    logger.warning("the ECB reference file carried no usable %s row", wanted)
    return None


def _parse_date(stamp: str) -> date | None:
    """Read a ``Cube time='YYYY-MM-DD'`` attribute.

    Args:
        stamp: The attribute value.

    Returns:
        The date, or None when it is not shaped like one.
    """
    try:
        return datetime.strptime(stamp.strip(), _DATE_FORMAT).date()  # noqa: DTZ007  (a date, not a moment)
    except ValueError:
        logger.warning("unexpected ECB date %r", stamp[:20])
        return None


def _decimal(raw: str) -> Decimal | None:
    """Read a plain ``1.2345`` rate attribute.

    Args:
        raw: The attribute value.

    Returns:
        The value, or None when it is not a number.
    """
    try:
        return Decimal(raw.strip())
    except InvalidOperation:
        return None


def fetch_rate(fetcher: PoliteFetcher) -> FxRate | None:
    """Download and parse the current ECB reference rates.

    Args:
        fetcher: The project's polite fetcher.

    Returns:
        The EUR/CZK rate, or None when the file could not be read.
    """
    return parse_rate(fetcher.get(ECB_URL).text)
