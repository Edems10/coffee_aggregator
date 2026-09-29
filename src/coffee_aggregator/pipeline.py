from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from coffee_aggregator.config import DEFAULT_BATCH_SIZE
from coffee_aggregator.fx import convert
from coffee_aggregator.http import FetchDisallowed, FetchError, FetchResult
from coffee_aggregator.money import per_kg

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from coffee_aggregator.db.monitoring import RunMonitor
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
#: How many detail pages one listing page is worth when a shop's cost is
#: estimated for sharding. Twenty-four is the page size the platforms this
#: project speaks — Shoptet, WooCommerce, Shopify — all default to.
PRODUCTS_PER_LISTING_PAGE = 24


@dataclass(slots=True, frozen=True)
class Deadline:
    """A wall-clock budget for one crawl.

    A shop that stops answering mid-page can hold a thread for the better part
    of an hour, and a sharded Lambda has fifteen minutes in total. The budget is
    therefore checked at batch and page boundaries — never in the middle of one,
    so nothing is written half-way — and a run that hits it is reported
    incomplete rather than raising.

    Attributes:
        at: The :func:`time.monotonic` reading the run must be over by.
    """

    at: float

    @classmethod
    def after(cls, seconds: float) -> Deadline:
        """Build a deadline that many seconds from now.

        Args:
            seconds: The budget.

        Returns:
            The deadline.
        """
        return cls(at=time.monotonic() + seconds)

    @property
    def remaining_s(self) -> float:
        """How long is left, never below zero.

        Returns:
            The remaining seconds.
        """
        return max(0.0, self.at - time.monotonic())

    def expired(self) -> bool:
        """Whether the budget is spent.

        Returns:
            True once the deadline has passed.
        """
        return time.monotonic() >= self.at


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
    started_at: datetime | None = None
    finished_at: datetime | None = None
    complete: bool = True
    discovery_ok: bool = True
    #: Set when the run stopped on its ``--deadline`` rather than on the end of
    #: the catalogue. Kept apart from ``complete``, which any single failed page
    #: also clears: this one alone says "we never saw the rest".
    deadline_reached: bool = False
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
    deadline: Deadline | None = None,
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
        deadline: The run's budget; walking the listings stops at the next
            reference once it is spent.

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
        if deadline is not None and deadline.expired():
            _record_deadline(site, report, "discovery")
            return
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


def _record_deadline(site: SiteAdapter, report: RunReport, where: str) -> None:
    """Record that the run ran out of time, without calling it a failure.

    Nothing went wrong — the shop is simply bigger than the budget — so this
    costs no ``failed`` count. It does clear ``complete`` and set
    :attr:`RunReport.deadline_reached`, which is what stops :func:`_may_delist`
    from treating the fraction of the catalogue we did see as the whole of it.

    Args:
        site: The shop that was being crawled.
        report: The report to mark.
        where: Which loop stopped, for the log line.
    """
    if report.deadline_reached:
        return
    report.deadline_reached = True
    report.complete = False
    logger.warning("%s: deadline reached during %s; stopping early", site.site_id, where)


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

    A capped run never delists, and neither does one that stopped on its
    deadline: both saw a prefix of the catalogue, and the rest is unseen rather
    than gone. An uncapped run does as long as discovery itself completed and
    only a small share of the products it found could not be read — otherwise a
    single transient 503 on a 500-product shop would keep ``delisted_at`` NULL
    forever.

    Args:
        report: The report of the run that just finished.
        limit: The ``--limit`` the caller passed, if any.
        max_pages: The ``--max-pages`` the caller passed, if any.

    Returns:
        True when ``mark_delisted`` may be called.
    """
    if limit is not None or max_pages is not None or not report.discovery_ok:
        return False
    if report.deadline_reached:
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
    batch_size: int = DEFAULT_BATCH_SIZE,
    fx_rate: FxRate | None = None,
    deadline: Deadline | None = None,
    monitor: RunMonitor | None = None,
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
        deadline: When the whole crawl must be over. Checked at batch and page
            boundaries; a run that hits it is reported incomplete.
        monitor: Where the finished report is recorded, when anywhere.

    Returns:
        A report of what happened; one bad product never aborts the run.
    """
    started = time.monotonic()
    report = RunReport(site_id=site.site_id, started_at=datetime.now(UTC))
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
        deadline=deadline,
    )

    if seen and _may_delist(report, limit=limit, max_pages=max_pages):
        report.delisted = sink.mark_delisted(site.site_id, seen)
    else:
        logger.info("%s: not delisting (partial or unhealthy run)", site.site_id)

    report.duration_s = time.monotonic() - started
    report.finished_at = datetime.now(UTC)
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
    if monitor is not None:
        monitor.record(report)
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
    deadline: Deadline | None = None,
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
        deadline: When the crawl must be over, checked between batches.

    Returns:
        The external ids that were parsed successfully.
    """
    seen: set[str] = set()
    refs: Iterator[ProductRef] = _safe_discover(
        site, fetcher, report, max_pages=max_pages, deadline=deadline
    )
    if limit is not None:
        refs = islice(refs, limit)

    for batch in _batched_refs(refs, max(1, batch_size)):
        # Between batches, not inside one: a half-written batch would report
        # products it never stored.
        if deadline is not None and deadline.expired():
            _record_deadline(site, report, "fetching")
            break
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

    ``price_per_kg`` falls out of the product's :attr:`~Coffee.price_basis` on
    its own; the normalised price fields need the day's fixing, which is why this
    is a pipeline step and not a model property — an adapter must never see a
    rate, let alone fetch one. Without a rate the fields stay NULL and the crawl
    is otherwise unaffected.

    Every per-kilogram figure is extrapolated from one basis — one price and the
    weight of the very package that price is for — and only then converted, so a
    250 g price can never be divided by a kilogram stated somewhere else on the
    page. A variant's own per-kilogram prices come from its own two numbers.

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
    basis = coffee.price_basis
    if basis is not None:
        amount = basis.amount_per_kg()
        coffee.price_per_kg_eur = convert.to_eur(amount, basis.currency, fx_rate)
        coffee.price_per_kg_czk = convert.to_czk(amount, basis.currency, fx_rate)
    for variant in coffee.variants:
        currency = variant.currency or coffee.currency
        variant.price_eur = convert.to_eur(variant.price, currency, fx_rate)
        variant.price_czk = convert.to_czk(variant.price, currency, fx_rate)
        variant_per_kg = per_kg(variant.price, variant.weight_g)
        variant.price_per_kg_eur = convert.to_eur(variant_per_kg, currency, fx_rate)
        variant.price_per_kg_czk = convert.to_czk(variant_per_kg, currency, fx_rate)


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


def run_many(  # noqa: PLR0913  (the same knobs as run, plus the shop pool)
    site_list: Sequence[SiteAdapter],
    fetcher: PoliteFetcher,
    sink: Sink,
    *,
    site_workers: int = 1,
    limit: int | None = None,
    max_pages: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    fx_rate: FxRate | None = None,
    deadline: Deadline | None = None,
    monitor: RunMonitor | None = None,
) -> list[RunReport]:
    """Crawl several shops, a few of them at the same time.

    A crawl spends nearly all of its time waiting for shops to answer, so the
    shops run in threads. Nothing about politeness changes: the rate limiter is
    keyed by host, and two shops are two hosts, so each one is still asked at
    its own pace. Running one shop twice as fast is what would be rude; running
    twenty shops at once is not.

    Args:
        site_list: The shops to crawl.
        fetcher: The shared polite fetcher, whose limiter keeps each host honest.
        sink: Where parsed coffees go; it is written to from several threads.
        site_workers: How many shops may be in flight at once.
        limit: Stop each shop after this many products (makes its run partial).
        max_pages: Cap each shop's listing pages (makes its run partial).
        batch_size: How many products are fetched and written per round.
        fx_rate: The day's rate, for the converted prices.
        deadline: One budget for the whole pool, not one per shop: the shops
            that are still running when it passes stop at their next boundary
            and the ones never started report nothing at all.
        monitor: Where each finished report is recorded, when anywhere.

    Returns:
        One report per shop, in the order the shops were given. A shop that
        raises is reported as a failed run rather than taking the others down.
    """
    workers = max(1, min(site_workers, len(site_list)))
    if workers == 1:
        return [
            run(
                site,
                fetcher,
                sink,
                limit=limit,
                max_pages=max_pages,
                batch_size=batch_size,
                fx_rate=fx_rate,
                deadline=deadline,
                monitor=monitor,
            )
            for site in site_list
        ]

    logger.info("crawling %d shops, %d at a time", len(site_list), workers)

    def crawl(site: SiteAdapter) -> RunReport:
        try:
            return run(
                site,
                fetcher,
                sink,
                limit=limit,
                max_pages=max_pages,
                batch_size=batch_size,
                fx_rate=fx_rate,
                deadline=deadline,
                monitor=monitor,
            )
        except Exception as exc:  # one shop never stops the rest
            logger.exception("%s: crawl failed", site.site_id)
            now = datetime.now(UTC)
            report = RunReport(site_id=site.site_id, started_at=now, finished_at=now)
            report.add_error(f"{type(exc).__name__}: {exc}")
            report.complete = False
            report.discovery_ok = False
            if monitor is not None:
                monitor.record(report)
            return report

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="site") as pool:
        return list(pool.map(crawl, site_list))


def host_of(site: SiteAdapter) -> str:
    """Return the host a shop is served from, folded for comparison.

    Args:
        site: Any registered adapter.

    Returns:
        The lower-case host without ``www.``, or the site id when the adapter
        declares no usable base URL.
    """
    host = urlsplit(site.base_url).netloc.lower().removeprefix("www.")
    return host or site.site_id


def estimated_requests(site: SiteAdapter, *, max_pages: int | None = None) -> int:
    """Estimate how many requests one shop costs.

    There is no catalogue size to read before the crawl, so the estimate is the
    only thing a shard split can be balanced on: every listing page the adapter
    is allowed to walk, plus the detail pages that page is expected to hold.

    Args:
        site: The adapter to weigh.
        max_pages: The run's own cap, when it is lower than the adapter's.

    Returns:
        The estimated number of requests, at least one.
    """
    pages = site.max_pages if max_pages is None else min(site.max_pages, max_pages)
    return max(1, pages * (1 + PRODUCTS_PER_LISTING_PAGE))


@dataclass(slots=True, frozen=True)
class HostCost:
    """What one host is expected to cost a run, and which shops it holds."""

    host: str
    site_ids: tuple[str, ...]
    requests: int
    delay_s: float

    @property
    def seconds(self) -> float:
        """The estimated wall-clock cost of walking this host.

        Returns:
            Requests times the delay each one has to wait out.
        """
        return self.requests * self.delay_s


class ShardError(ValueError):
    """Raised when a ``--shard i/n`` selection makes no sense."""


def host_costs(
    site_list: Sequence[SiteAdapter],
    *,
    delay_for: Callable[[str], float],
    max_pages: int | None = None,
) -> list[HostCost]:
    """Group shops by host and estimate what each host costs.

    Two shops on one origin are one unit of work, not two: the rate limiter is
    keyed by host, so splitting them across shards would have each shard pace
    that origin on its own and double the request rate the shop sees.

    Args:
        site_list: The shops to weigh.
        delay_for: The delay one host has to be asked at, crawl-delay included.
        max_pages: The run's own listing-page cap, when it has one.

    Returns:
        One entry per host, most expensive first, ties broken by host name.
    """
    grouped: dict[str, list[SiteAdapter]] = {}
    for site in site_list:
        grouped.setdefault(host_of(site), []).append(site)
    costs = [
        HostCost(
            host=host,
            site_ids=tuple(sorted(shop.site_id for shop in shops)),
            requests=sum(estimated_requests(shop, max_pages=max_pages) for shop in shops),
            delay_s=delay_for(host),
        )
        for host, shops in grouped.items()
    ]
    costs.sort(key=lambda cost: (-cost.seconds, cost.host))
    return costs


def shard_hosts(
    costs: Sequence[HostCost],
    index: int,
    count: int,
) -> list[HostCost]:
    """Pick one shard's worth of hosts, balanced by cost rather than by count.

    Longest-processing-time first: the most expensive host goes to the emptiest
    shard, then the next, and so on. Counting shops instead would put fifty fast
    shops in one shard and a single 30-second-crawl-delay shop in another, which
    is exactly the split that misses the deadline.

    Args:
        costs: Every host's cost, as :func:`host_costs` returns them.
        index: Which shard to return, counting from one.
        count: How many shards the run is split into.

    Returns:
        The hosts belonging to shard ``index``, most expensive first.

    Raises:
        ShardError: When the shard numbers are out of range.
    """
    if count < 1:
        message = f"a shard count must be at least 1, got {count}"
        raise ShardError(message)
    if not 1 <= index <= count:
        message = f"shard {index} does not exist in a split of {count}"
        raise ShardError(message)
    bins: list[list[HostCost]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for cost in costs:
        target = min(range(count), key=lambda slot: (loads[slot], slot))
        bins[target].append(cost)
        loads[target] += cost.seconds
    return bins[index - 1]


def shard_sites(
    site_list: Sequence[SiteAdapter],
    index: int,
    count: int,
    *,
    delay_for: Callable[[str], float],
    max_pages: int | None = None,
) -> list[SiteAdapter]:
    """Return the shops of one shard, in the order they were given.

    Args:
        site_list: Every shop the run would otherwise crawl.
        index: Which shard to return, counting from one.
        count: How many shards the run is split into.
        delay_for: The delay one host has to be asked at.
        max_pages: The run's own listing-page cap, when it has one.

    Returns:
        The shops of that shard.
    """
    costs = host_costs(site_list, delay_for=delay_for, max_pages=max_pages)
    chosen = {site_id for cost in shard_hosts(costs, index, count) for site_id in cost.site_ids}
    return [site for site in site_list if site.site_id in chosen]


def wrote_nothing(reports: Sequence[RunReport]) -> list[str]:
    """Name the shops that stored not one product.

    Args:
        reports: What the run produced.

    Returns:
        The site ids that wrote nothing, sorted.
    """
    return sorted(report.site_id for report in reports if report.written == 0)
