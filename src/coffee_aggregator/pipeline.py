from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from itertools import islice
from typing import TYPE_CHECKING

from coffee_aggregator.fx import convert
from coffee_aggregator.http import FetchDisallowed, FetchError, FetchResult

if TYPE_CHECKING:
    from collections.abc import Iterator

    from coffee_aggregator.fx import FxRate
    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee
    from coffee_aggregator.sinks.base import Sink
    from coffee_aggregator.sites.base import ProductRef, SiteAdapter

logger = logging.getLogger(__name__)

MAX_REPORTED_ERRORS = 50
PROGRESS_EVERY = 20
#: Share of the discovered products that may be lost (fetch failure, robots
#: refusal, parse error) before a full run is considered too unhealthy to base
#: delisting on.
MAX_LOST_FRACTION = 0.1


@dataclass(slots=True)
class RunReport:
    """What one crawl of one shop did."""

    site_id: str
    discovered: int = 0
    fetched: int = 0
    parsed: int = 0
    skipped_non_coffee: int = 0
    failed: int = 0
    disallowed: int = 0
    written: int = 0
    delisted: int = 0
    duration_s: float = 0.0
    complete: bool = True
    discovery_ok: bool = True
    errors: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        """Record an error, keeping only the first :data:`MAX_REPORTED_ERRORS`.

        Args:
            message: What went wrong, including the URL.
        """
        self.failed += 1
        if len(self.errors) < MAX_REPORTED_ERRORS:
            self.errors.append(message)


def _batched_refs(refs: Iterator[ProductRef], size: int) -> Iterator[list[ProductRef]]:
    while True:
        chunk = list(islice(refs, size))
        if not chunk:
            return
        yield chunk


def _safe_discover(
    site: SiteAdapter,
    fetcher: PoliteFetcher,
    report: RunReport,
    *,
    max_pages: int | None = None,
) -> Iterator[ProductRef]:
    """Iterate a site's discovery, turning any listing failure into a report entry.

    Adapters are generators, so a failing listing page surfaces while the pipeline
    iterates — not when ``discover()`` is called. Either way the run ends up marked
    incomplete instead of raising: markup changes without warning, and one shop
    whose listing selectors rotted must never abort a ``--site all`` crawl.

    Args:
        site: The shop adapter being crawled.
        fetcher: The shared polite fetcher.
        report: The report to record the failure in.
        max_pages: Cap on listing pages, passed straight to the adapter.

    Yields:
        Every product reference the adapter managed to produce.
    """
    try:
        refs = iter(site.discover(fetcher, max_pages=max_pages))
    except FetchDisallowed as exc:
        _record_discovery_refusal(site, report, exc)
        return
    except Exception as exc:  # noqa: BLE001  (a broken listing must never abort a crawl)
        _record_discovery_failure(site, report, exc)
        return
    while True:
        try:
            ref = next(refs)
        except StopIteration:
            return
        except FetchDisallowed as exc:
            _record_discovery_refusal(site, report, exc)
            return
        except Exception as exc:  # noqa: BLE001  (a broken listing must never abort a crawl)
            _record_discovery_failure(site, report, exc)
            return
        yield ref


def _record_discovery_failure(site: SiteAdapter, report: RunReport, exc: Exception) -> None:
    report.add_error(f"discovery failed for {site.site_id}: {type(exc).__name__}: {exc}")
    report.complete = False
    report.discovery_ok = False
    logger.warning("%s: discovery stopped early: %s", site.site_id, exc)


def _record_discovery_refusal(site: SiteAdapter, report: RunReport, exc: FetchDisallowed) -> None:
    """Record a listing page robots.txt refuses.

    A refusal is not a fault of ours and not an error to retry: it is counted
    apart from ``failed`` so a shop that closes a category never inflates the
    failure rate, while still keeping the run partial so nothing is delisted.

    Args:
        site: The shop adapter being crawled.
        report: The report to record the refusal in.
        exc: The refusal the fetcher raised.
    """
    report.disallowed += 1
    report.complete = False
    report.discovery_ok = False
    logger.warning("%s: robots.txt disallows %s", site.site_id, exc.url)


def _may_delist(report: RunReport, *, limit: int | None, max_pages: int | None) -> bool:
    """Decide whether this run saw enough of the catalogue to delist the rest.

    A capped run never delists. An uncapped run does as long as discovery itself
    completed and only a small share of the products it found could not be read —
    otherwise a single transient 503 on a 500-product shop would keep
    ``delisted_at`` NULL forever.

    Args:
        report: The report of the run that just finished.
        limit: The ``--limit`` the caller passed, if any.
        max_pages: The ``--max-pages`` the caller passed, if any.

    Returns:
        True when ``mark_delisted`` may be called.
    """
    if limit is not None or max_pages is not None or not report.discovery_ok:
        return False
    if not report.discovered:
        return False
    lost = report.failed + report.disallowed
    return lost <= report.discovered * MAX_LOST_FRACTION


def run(  # noqa: PLR0913  (the contract fixes this signature)
    site: SiteAdapter,
    fetcher: PoliteFetcher,
    sink: Sink,
    *,
    limit: int | None = None,
    max_pages: int | None = None,
    batch_size: int = 50,
    fx_rate: FxRate | None = None,
) -> RunReport:
    """Crawl one shop end to end.

    Args:
        site: The shop adapter to run.
        fetcher: The shared polite fetcher.
        sink: Where parsed coffees go.
        limit: Stop after this many products (makes the run partial).
        max_pages: Cap on listing pages (makes the run partial).
        batch_size: How many products are fetched and written per round.
        fx_rate: The day's EUR/CZK fixing, used to fill the normalised prices.
            None leaves them NULL and the crawl proceeds unchanged.

    Returns:
        A report of what happened; one bad product never aborts the run.
    """
    started = time.monotonic()
    report = RunReport(site_id=site.site_id)
    report.complete = limit is None and max_pages is None

    # Adapters are registry singletons, so the cap travels as an argument and is
    # never written onto one: mutating ``site.max_pages`` would make the next run
    # in the same process crawl a truncated catalogue and then delist the rest.
    seen = _crawl(
        site,
        fetcher,
        sink,
        report,
        limit=limit,
        max_pages=max_pages,
        batch_size=batch_size,
        fx_rate=fx_rate,
    )

    if seen and _may_delist(report, limit=limit, max_pages=max_pages):
        report.delisted = sink.mark_delisted(site.site_id, seen)
    else:
        logger.info("%s: not delisting (partial or unhealthy run)", site.site_id)

    report.duration_s = time.monotonic() - started
    logger.info(
        "%s: discovered=%d fetched=%d parsed=%d written=%d failed=%d in %.1fs",
        site.site_id,
        report.discovered,
        report.fetched,
        report.parsed,
        report.written,
        report.failed,
        report.duration_s,
    )
    return report


def _crawl(  # noqa: PLR0913  (one linear crawl loop reads better than five helpers)
    site: SiteAdapter,
    fetcher: PoliteFetcher,
    sink: Sink,
    report: RunReport,
    *,
    limit: int | None,
    max_pages: int | None,
    batch_size: int,
    fx_rate: FxRate | None = None,
) -> set[str]:
    """Discover, fetch, parse and store every product of one shop.

    Args:
        site: The shop adapter to run.
        fetcher: The shared polite fetcher.
        sink: Where parsed coffees go.
        report: The report to fill in.
        limit: Stop after this many products.
        max_pages: Cap on listing pages, passed straight to the adapter.
        batch_size: How many products are fetched and written per round.
        fx_rate: The day's EUR/CZK fixing, or None.

    Returns:
        The external ids that were parsed successfully.
    """
    seen: set[str] = set()
    refs: Iterator[ProductRef] = _safe_discover(site, fetcher, report, max_pages=max_pages)
    if limit is not None:
        refs = islice(refs, limit)

    for batch in _batched_refs(refs, max(1, batch_size)):
        report.discovered += len(batch)
        # Never spend a request on a product the listing already names as
        # cascara, merchandise or a tasting pack.
        wanted = [ref for ref in batch if not site.is_ignored(ref.name)]
        report.skipped_non_coffee += len(batch) - len(wanted)
        if not wanted:
            continue
        results = _retrieve(fetcher, wanted)
        coffees = _parse_batch(site, report, wanted, results)
        for coffee in coffees:
            derive(coffee, fx_rate)
            seen.add(coffee.external_id)
        if coffees:
            outcome = sink.upsert(coffees)
            report.written += outcome.written
            report.failed += outcome.failed
    return seen


def _retrieve(
    fetcher: PoliteFetcher,
    refs: list[ProductRef],
) -> list[FetchResult | FetchError | FetchDisallowed]:
    """Get the source of every reference, requesting only the ones that need it.

    A JSON-API or feed-driven shop already holds the product's own source after
    discovery; such a reference carries it in ``payload`` and costs no request.

    Args:
        fetcher: The shared polite fetcher.
        refs: The references of one batch, in order.

    Returns:
        One entry per reference, in the same order.
    """
    pending = [ref.url for ref in refs if ref.payload is None]
    fetched = iter(fetcher.fetch_many(pending) if pending else ())
    results: list[FetchResult | FetchError | FetchDisallowed] = []
    for ref in refs:
        if ref.payload is None:
            results.append(next(fetched))
            continue
        results.append(
            FetchResult(
                url=ref.url,
                final_url=ref.url,
                status=200,
                text=ref.payload,
                from_cache=True,
                elapsed_s=0.0,
            )
        )
    return results


def _parse_batch(
    site: SiteAdapter,
    report: RunReport,
    refs: list[ProductRef],
    results: list[FetchResult | FetchError | FetchDisallowed],
) -> list[Coffee]:
    """Parse one fetched batch in the calling thread.

    Args:
        site: The shop adapter to parse with.
        report: The report to fill in.
        refs: The references that were fetched, in order.
        results: What the fetcher returned for each of them.

    Returns:
        The coffees that parsed successfully.
    """
    coffees: list[Coffee] = []
    for ref, result in zip(refs, results, strict=True):
        if isinstance(result, FetchDisallowed):
            report.disallowed += 1
            report.complete = False
            logger.warning("robots.txt disallows %s", ref.url)
            continue
        if isinstance(result, FetchError):
            report.add_error(f"fetch failed for {ref.url}: {result.reason}")
            report.complete = False
            continue
        report.fetched += 1
        try:
            coffee = site.parse_product(result.text, ref)
        except Exception as exc:  # noqa: BLE001  (a broken page must never abort a crawl)
            report.add_error(f"parse failed for {ref.url}: {type(exc).__name__}: {exc}")
            report.complete = False
            continue
        if coffee is None:
            report.skipped_non_coffee += 1
            continue
        _merge_listing_data(coffee, ref)
        report.parsed += 1
        coffees.append(coffee)
        if report.parsed % PROGRESS_EVERY == 0:
            logger.info("%s: parsed %d products", site.site_id, report.parsed)
    return coffees


def derive(coffee: Coffee, fx_rate: FxRate | None = None) -> None:
    """Fill everything computed rather than scraped, in place.

    ``price_per_kg`` falls out of price and weight on its own; the six normalised
    price fields need the day's fixing, which is why this is a pipeline step and
    not a model property — an adapter must never see a rate, let alone fetch one.
    Without a rate the fields stay NULL and the crawl is otherwise unaffected.

    Args:
        coffee: The freshly parsed product, modified in place.
        fx_rate: The day's EUR/CZK fixing, or None.
    """
    if fx_rate is None:
        return
    coffee.fx_rate_eur_czk = float(fx_rate.rate)
    coffee.fx_date = fx_rate.date
    coffee.price_eur = convert.to_eur(coffee.price, coffee.currency, fx_rate)
    coffee.price_czk = convert.to_czk(coffee.price, coffee.currency, fx_rate)
    per_kg = coffee.price_per_kg
    coffee.price_per_kg_eur = convert.to_eur(per_kg, coffee.currency, fx_rate)
    coffee.price_per_kg_czk = convert.to_czk(per_kg, coffee.currency, fx_rate)
    for variant in coffee.variants:
        currency = variant.currency or coffee.currency
        variant.price_eur = convert.to_eur(variant.price, currency, fx_rate)
        variant.price_czk = convert.to_czk(variant.price, currency, fx_rate)


def _merge_listing_data(coffee: Coffee, ref: ProductRef) -> None:
    """Keep what only the listing card knew, prefixed so its origin stays visible.

    Listing cards routinely carry data the detail page never repeats — flavour
    icons, stock wording, promo flags. ``setdefault`` means the detail page's own
    value always wins when both state the same thing.

    Args:
        coffee: The coffee just parsed from the detail page.
        ref: The listing reference it was parsed for.
    """
    for key, value in ref.extra.items():
        coffee.raw_attributes.setdefault(f"LIST_{key.upper()}", value)
