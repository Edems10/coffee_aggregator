from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from decimal import Decimal

    from coffee_aggregator.http import PoliteFetcher

logger = logging.getLogger(__name__)

#: The only pair the project needs: every shop prices in one of the two.
BASE_CURRENCY = "EUR"
QUOTE_CURRENCY = "CZK"
#: ``date.weekday()`` of the first day the Czech National Bank publishes nothing.
_SATURDAY = 5


@dataclass(slots=True, frozen=True)
class FxRate:
    """One daily fixing: how many ``quote`` one ``base`` buys."""

    date: date
    rate: Decimal
    source: str
    base: str = BASE_CURRENCY
    quote: str = QUOTE_CURRENCY

    def to_record(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of this rate.

        Returns:
            A flat dictionary; the rate is a string so no precision is lost.
        """
        return {
            "date": self.date.isoformat(),
            "base": self.base,
            "quote": self.quote,
            "rate": str(self.rate),
            "source": self.source,
        }


class FxSource(Protocol):
    """One authoritative feed the rate can be read from."""

    def __call__(self, fetcher: PoliteFetcher) -> FxRate | None:
        """Fetch today's fixing.

        Args:
            fetcher: The project's polite fetcher; the only way out to the net.

        Returns:
            The fixing, or None when the feed carried no usable EUR/CZK row.
        """


class FxStore(Protocol):
    """Where fetched rates are kept between runs."""

    def get_latest(self) -> FxRate | None:
        """Return the newest rate held.

        Returns:
            The rate with the largest date, or None when nothing is stored.
        """

    def get(self, day: date) -> FxRate | None:
        """Return the rate of one day.

        Args:
            day: The fixing date wanted.

        Returns:
            The rate, or None when that day is not stored.
        """

    def put(self, rate: FxRate) -> None:
        """Store one rate, replacing any rate already held for its date.

        Args:
            rate: The rate to keep.
        """


def latest_expected_fixing(day: date) -> date:
    """Roll a date back to the most recent day a fixing can exist for.

    The Czech National Bank publishes on working days only and keeps serving the
    last one over the weekend. Asking for Saturday's rate therefore means asking
    for Friday's, and a Friday rate already in the store spares us a request.

    Args:
        day: The day the caller is crawling on.

    Returns:
        ``day`` itself on a weekday, otherwise the Friday before it.
    """
    while day.weekday() >= _SATURDAY:
        day -= timedelta(days=1)
    return day


class FxService:
    """Hands out today's EUR/CZK rate, fetching it at most once a day."""

    def __init__(
        self,
        fetcher: PoliteFetcher,
        store: FxStore,
        sources: Sequence[FxSource] | None = None,
    ) -> None:
        """Build the service.

        Args:
            fetcher: The polite fetcher every feed request goes through.
            store: Where fetched rates are persisted.
            sources: The feeds to try, in order; defaults to CNB then ECB.
        """
        self.fetcher = fetcher
        self.store = store
        self.sources: Sequence[FxSource] = default_sources() if sources is None else tuple(sources)

    def get_rate(self, today: date | None = None, *, refresh: bool = False) -> FxRate | None:
        """Return the rate to stamp on this run, never raising into the crawl.

        The store is asked first, so a second crawl on the same day costs no
        request. When no feed answers, the newest stored rate is handed back with
        a warning that says how old it is — a slightly stale rate still makes two
        shops comparable, whereas no rate at all makes the whole run incomparable.

        Args:
            today: The day being crawled; defaults to the current UTC date.
            refresh: Skip the store lookup and go to the feeds.

        Returns:
            The rate, or None when nothing could be fetched and nothing is stored.
        """
        day = today or datetime.now(UTC).date()
        wanted = latest_expected_fixing(day)
        if not refresh:
            stored = self._stored_for(wanted)
            if stored is not None:
                logger.debug("using the stored %s rate for %s", self._pair(), stored.date)
                return stored
        fetched = self._fetch()
        if fetched is not None:
            self._persist(fetched)
            return fetched
        return self._fallback(day)

    def _pair(self) -> str:
        return f"{BASE_CURRENCY}/{QUOTE_CURRENCY}"

    def _stored_for(self, wanted: date) -> FxRate | None:
        """Return a stored rate that is already as fresh as a fetch could be.

        Args:
            wanted: The most recent day a fixing can exist for.

        Returns:
            The rate, or None when the store holds nothing that recent.
        """
        same_day = self._safe(lambda: self.store.get(wanted), "read")
        if same_day is not None:
            return same_day
        latest = self._safe(self.store.get_latest, "read")
        return latest if latest is not None and latest.date >= wanted else None

    def _fetch(self) -> FxRate | None:
        """Try every feed in order.

        Returns:
            The first rate a feed yielded, or None when all of them failed.
        """
        for source in self.sources:
            name = getattr(source, "__name__", type(source).__name__)
            try:
                rate = source(self.fetcher)
            except Exception:  # a broken feed must never abort a crawl
                logger.warning("%s rate source %s failed", self._pair(), name, exc_info=True)
                continue
            if rate is not None:
                logger.info(
                    "%s = %s on %s (source: %s)",
                    self._pair(),
                    rate.rate,
                    rate.date,
                    rate.source,
                )
                return rate
            logger.warning("%s rate source %s carried no usable row", self._pair(), name)
        return None

    def _persist(self, rate: FxRate) -> None:
        self._safe(lambda: self.store.put(rate), "write")

    def _fallback(self, day: date) -> FxRate | None:
        """Fall back to the newest stored rate when no feed answered.

        Args:
            day: The day being crawled, to state the age of what is returned.

        Returns:
            The stored rate, or None when the store is empty.
        """
        latest = self._safe(self.store.get_latest, "read")
        if latest is None:
            logger.warning(
                "no %s rate could be fetched and none is stored; prices stay in their own currency",
                self._pair(),
            )
            return None
        logger.warning(
            "no %s rate could be fetched; falling back to the one from %s, %d day(s) old",
            self._pair(),
            latest.date,
            (day - latest.date).days,
        )
        return latest

    def _safe[T](self, call: Callable[[], T], what: str) -> T | None:
        """Run a store call, turning a broken store into a warning.

        Args:
            call: The store operation to run.
            what: ``"read"`` or ``"write"``, for the log line.

        Returns:
            What the store returned, or None when it raised.
        """
        try:
            return call()
        except Exception:  # a broken store must never abort a crawl
            logger.warning("the %s rate store could not %s", self._pair(), what, exc_info=True)
            return None


def default_sources() -> tuple[FxSource, ...]:
    """Return the feeds tried in order: the Czech National Bank, then the ECB.

    Returns:
        The source callables.
    """
    from coffee_aggregator.fx import cnb, ecb  # noqa: PLC0415  (avoids an import cycle)

    return (cnb.fetch_rate, ecb.fetch_rate)
