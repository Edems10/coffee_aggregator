from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.sites.base import ProductRef

logger = logging.getLogger(__name__)

__all__ = ["walk_listing"]


def walk_listing(
    fetcher: PoliteFetcher,
    urls: Iterable[str],
    parse: Callable[[str], list[ProductRef]],
    *,
    stop_when_stale: bool = True,
) -> Iterator[ProductRef]:
    """Walk a shop's listing URLs and yield every product exactly once.

    Two rules end the walk, and both were learned the hard way. A listing page
    past the last one does not 404: the shop redirects it to page one, so a walk
    that only counted pages re-read page one until ``max_pages`` ran out. And a
    shop that pads its last page repeats products rather than shrinking it, so a
    page that adds nothing new is the end of the catalogue.

    Args:
        fetcher: The shared polite fetcher.
        urls: The listing URLs to walk, lazily — nothing past the stop is built,
            so a page range may be as long as the cap allows.
        parse: Turns one listing page into references.
        stop_when_stale: Stop at the first page that adds no new product. True
            for a paginated category, where a stale page means the end; False
            for a fixed list of distinct categories, where one category may
            hold nothing the previous ones did not and the next still does.

    Yields:
        One reference per distinct product, in page order.
    """
    seen: set[str] = set()
    for index, url in enumerate(urls, start=1):
        result = fetcher.get(url)
        if result.final_url.rstrip("/") != url.rstrip("/"):
            logger.debug("listing page %d redirected to %s, stopping", index, result.final_url)
            return
        fresh = [ref for ref in parse(result.text) if ref.external_id not in seen]
        if not fresh and stop_when_stale:
            logger.debug("listing page %d added no new products, stopping", index)
            return
        seen.update(ref.external_id for ref in fresh)
        yield from fresh
