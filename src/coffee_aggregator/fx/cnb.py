from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from coffee_aggregator.fx.rates import BASE_CURRENCY, FxRate

if TYPE_CHECKING:
    from coffee_aggregator.http import PoliteFetcher

logger = logging.getLogger(__name__)

#: The Czech National Bank's daily fixing, the authoritative CZK reference rate.
CNB_URL = (
    "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/"
    "kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"
)
SOURCE = "cnb"

_SEPARATOR = "|"
#: ``země|měna|množství|kód|kurz`` — five fields, the last two the ones we read.
_COLUMNS = 5
_AMOUNT_INDEX = 2
_CODE_INDEX = 3
_RATE_INDEX = 4
_DATE_FORMAT = "%d.%m.%Y"
#: Length of an ISO 4217 code, used to skip the header row and any stray line.
_CODE_LENGTH = 3


@dataclass(slots=True, frozen=True)
class Fixing:
    """One published day of the CNB table, as CZK per one unit of each currency."""

    date: date
    rates: dict[str, Decimal] = field(default_factory=dict)


def _decimal(raw: str) -> Decimal | None:
    """Read a number written with a decimal comma.

    Args:
        raw: The cell as published, e.g. ``"24,260"`` or ``"1 234,5"``.

    Returns:
        The value, or None when the cell is not a number.
    """
    cleaned = raw.strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_fixing(text: str) -> Fixing | None:
    """Parse the whole ``denni_kurz.txt`` table.

    The first line carries the date the fixing belongs to — on a weekend or a
    public holiday that is the last working day, and it is kept as published
    rather than replaced with today. Every rate is divided by its ``množství``
    column, so the currencies quoted per 100 units (JPY, HUF) come out per unit
    like all the others.

    Args:
        text: The file as served.

    Returns:
        The fixing, or None when the file carried no date or no usable row.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    day = _parse_date(lines[0])
    if day is None:
        return None
    rates: dict[str, Decimal] = {}
    for line in lines[1:]:
        parsed = _parse_row(line)
        if parsed is not None:
            rates[parsed[0]] = parsed[1]
    if not rates:
        logger.warning("the CNB fixing for %s carried no readable row", day)
        return None
    return Fixing(date=day, rates=rates)


def _parse_date(line: str) -> date | None:
    """Read the ``DD.MM.YYYY #NNN`` header line.

    Args:
        line: The first line of the file.

    Returns:
        The fixing date, or None when the line is not shaped like one.
    """
    head = line.strip().split("#", maxsplit=1)[0].strip()
    try:
        return datetime.strptime(head, _DATE_FORMAT).date()  # noqa: DTZ007  (a date, not a moment)
    except ValueError:
        logger.warning("unexpected first line in the CNB fixing: %r", line[:60])
        return None


def _parse_row(line: str) -> tuple[str, Decimal] | None:
    """Read one ``země|měna|množství|kód|kurz`` row.

    Args:
        line: The row, header line included (which is skipped).

    Returns:
        The ISO code and the CZK price of one unit, or None for the header and
        for anything unreadable.
    """
    cells = line.split(_SEPARATOR)
    if len(cells) != _COLUMNS:
        return None
    amount = _decimal(cells[_AMOUNT_INDEX])
    rate = _decimal(cells[_RATE_INDEX])
    code = cells[_CODE_INDEX].strip().upper()
    if amount is None or rate is None or not amount or len(code) != _CODE_LENGTH:
        return None
    return code, rate / amount


def parse_rate(text: str, code: str = BASE_CURRENCY) -> FxRate | None:
    """Pull one currency's rate out of the CNB table.

    Args:
        text: The file as served.
        code: The ISO code wanted; the project only ever asks for EUR.

    Returns:
        The rate, or None when the file is unusable or lists no such currency.
    """
    fixing = parse_fixing(text)
    if fixing is None:
        return None
    rate = fixing.rates.get(code.upper())
    if rate is None:
        logger.warning("the CNB fixing for %s lists no %s row", fixing.date, code)
        return None
    return FxRate(date=fixing.date, rate=rate, source=SOURCE, base=code.upper())


def fetch_rate(fetcher: PoliteFetcher) -> FxRate | None:
    """Download and parse the current CNB fixing.

    Args:
        fetcher: The project's polite fetcher; robots.txt and the per-host delay
            apply to the bank exactly as they do to a shop.

    Returns:
        The EUR/CZK rate, or None when the file could not be read.
    """
    return parse_rate(fetcher.get(CNB_URL).text)
