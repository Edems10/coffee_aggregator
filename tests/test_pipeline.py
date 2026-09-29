from __future__ import annotations

import threading
import time
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest

from coffee_aggregator.fx import FxRate
from coffee_aggregator.http import FetchDisallowed, FetchError, FetchResult
from coffee_aggregator.models import Variant
from coffee_aggregator.pipeline import (
    MAX_REPORTED_ERRORS,
    PRODUCTS_PER_LISTING_PAGE,
    Deadline,
    RunReport,
    ShardError,
    derive,
    estimated_requests,
    host_costs,
    host_of,
    run,
    run_many,
    shard_hosts,
    shard_sites,
    wrote_nothing,
)
from coffee_aggregator.sinks.base import SinkResult
from coffee_aggregator.sites.base import ProductRef, SiteAdapter
from conftest import make_coffee

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee
    from coffee_aggregator.sinks.base import Sink

BASE = "https://fake.example.sk"


class FakeSite(SiteAdapter):
    site_id = "fake"
    name = "Fake Roastery"
    country = "SK"
    base_url = BASE

    def __init__(
        self,
        count: int = 5,
        *,
        broken: set[str] | None = None,
        extra: dict[str, str] | None = None,
        payload: str | None = None,
    ) -> None:
        self.count = count
        self.broken = broken or set()
        self.extra = extra or {}
        self.payload = payload
        self.discovered: list[str] = []
        self.max_pages_seen: list[int | None] = []

    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        self.max_pages_seen.append(max_pages)
        for index in range(self.count):
            external_id = str(index)
            self.discovered.append(external_id)
            yield ProductRef(
                site_id=self.site_id,
                external_id=external_id,
                url=f"{BASE}/detail/{external_id}",
                name=f"Coffee {external_id}",
                extra=dict(self.extra),
                payload=self.payload,
            )

    def parse_product(self, html: str, ref: ProductRef) -> Coffee | None:
        if ref.external_id in self.broken:
            message = "no name on the page"
            raise ValueError(message)
        if html == "not-coffee":
            return None
        return make_coffee(site=self.site_id, external_id=ref.external_id)


class FakeFetcher:
    """Returns canned bodies (or errors) without any network."""

    def __init__(self, bodies: dict[str, str] | None = None) -> None:
        self.bodies = bodies or {}
        self.requested: list[str] = []

    def fetch_many(self, urls: Sequence[str]) -> list[FetchResult | FetchError | FetchDisallowed]:
        results: list[FetchResult | FetchError | FetchDisallowed] = []
        for url in urls:
            self.requested.append(url)
            body = self.bodies.get(url, "<html>ok</html>")
            if body == "__error__":
                results.append(FetchError(url, "HTTP 500"))
            elif body == "__disallowed__":
                results.append(FetchDisallowed(url))
            else:
                results.append(FetchResult(url, url, 200, body, from_cache=False, elapsed_s=0.01))
        return results


class FakeSink:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.written: list[Coffee] = []
        self.delisted: list[tuple[str, set[str]]] = []
        self.closed = False

    def upsert(self, coffees: Sequence[Coffee]) -> SinkResult:
        self.batches.append([coffee.external_id for coffee in coffees])
        self.written.extend(coffees)
        return SinkResult(written=len(coffees), failed=0)

    def mark_delisted(self, site_id: str, seen_external_ids: set[str]) -> int:
        self.delisted.append((site_id, set(seen_external_ids)))
        return len(seen_external_ids)

    def close(self) -> None:
        self.closed = True


def _run(site: FakeSite, fetcher: FakeFetcher, sink: FakeSink, **kwargs: object) -> RunReport:
    return run(
        site,
        cast("PoliteFetcher", fetcher),
        cast("Sink", sink),
        **kwargs,  # type: ignore[arg-type]
    )


def test_full_run_writes_everything_and_delists() -> None:
    site, fetcher, sink = FakeSite(3), FakeFetcher(), FakeSink()
    report = _run(site, fetcher, sink)

    assert report.site_id == "fake"
    assert report.discovered == 3
    assert report.fetched == 3
    assert report.parsed == 3
    assert report.written == 3
    assert report.failed == 0
    assert report.complete is True
    assert sink.delisted == [("fake", {"0", "1", "2"})]
    assert report.delisted == 3
    assert report.duration_s >= 0.0


def test_limit_stops_discovery_and_skips_delisting() -> None:
    site, fetcher, sink = FakeSite(10), FakeFetcher(), FakeSink()
    report = _run(site, fetcher, sink, limit=4)

    assert report.discovered == 4
    assert report.written == 4
    assert len(fetcher.requested) == 4
    assert report.complete is False
    assert sink.delisted == []


def test_max_pages_reaches_discover_without_mutating_the_adapter() -> None:
    """Adapters are registry singletons: the cap is an argument, never state."""
    site, fetcher, sink = FakeSite(2), FakeFetcher(), FakeSink()
    default_cap = site.max_pages
    report = _run(site, fetcher, sink, max_pages=1)

    assert site.max_pages_seen == [1]
    assert site.max_pages == default_cap
    assert report.complete is False
    assert sink.delisted == []


def test_an_uncapped_run_passes_no_cap_to_discover() -> None:
    site = FakeSite(2)
    _run(site, FakeFetcher(), FakeSink())
    assert site.max_pages_seen == [None]


def test_a_disallowed_listing_page_is_counted_apart_from_failures() -> None:
    class RefusedSite(FakeSite):
        def discover(
            self,
            fetcher: PoliteFetcher,
            *,
            max_pages: int | None = None,
        ) -> Iterator[ProductRef]:
            yield ProductRef(
                site_id=self.site_id,
                external_id="0",
                url=f"{BASE}/detail/0",
                name="Coffee 0",
            )
            refused = FetchDisallowed(f"{BASE}/list?page=2")
            raise refused

    sink = FakeSink()
    report = _run(RefusedSite(), FakeFetcher(), sink, batch_size=1)

    assert report.disallowed == 1
    assert report.failed == 0
    assert report.errors == []
    assert report.complete is False
    assert report.discovery_ok is False
    assert report.parsed == 1
    assert sink.delisted == []


def test_a_disallowed_listing_page_before_the_first_product_is_not_a_failure() -> None:
    class RefusedSite(FakeSite):
        def discover(
            self,
            fetcher: PoliteFetcher,
            *,
            max_pages: int | None = None,
        ) -> Iterator[ProductRef]:
            yield from ()
            refused = FetchDisallowed(f"{BASE}/list")
            raise refused

    report = _run(RefusedSite(), FakeFetcher(), FakeSink())
    assert (report.disallowed, report.failed, report.discovery_ok) == (1, 0, False)


def test_listing_only_data_is_merged_into_raw_attributes() -> None:
    site = FakeSite(1, extra={"flavor_notes": "kakao, karamel", "stock": "Skladom"})
    sink = FakeSink()
    _run(site, FakeFetcher(), sink)

    coffee = sink.written[0]
    assert coffee.raw_attributes["LIST_FLAVOR_NOTES"] == "kakao, karamel"
    assert coffee.raw_attributes["LIST_STOCK"] == "Skladom"


def test_the_detail_page_wins_over_the_listing_for_the_same_key() -> None:
    site = FakeSite(1, extra={"krajina": "z karty"})
    sink = FakeSink()
    _run(site, FakeFetcher(), sink)
    # make_coffee() already states KRAJINA, and LIST_* never collides with it
    assert sink.written[0].raw_attributes["KRAJINA"] == "Kuba"
    assert sink.written[0].raw_attributes["LIST_KRAJINA"] == "z karty"


def test_a_ref_carrying_its_payload_is_never_fetched() -> None:
    site = FakeSite(3, payload="<html>from the feed</html>")
    fetcher, sink = FakeFetcher(), FakeSink()
    report = _run(site, fetcher, sink)

    assert fetcher.requested == []
    assert report.parsed == 3
    assert report.written == 3
    assert report.failed == 0


def test_payload_and_fetched_refs_mix_in_one_batch() -> None:
    class MixedSite(FakeSite):
        def discover(
            self,
            fetcher: PoliteFetcher,
            *,
            max_pages: int | None = None,
        ) -> Iterator[ProductRef]:
            for index in range(3):
                yield ProductRef(
                    site_id=self.site_id,
                    external_id=str(index),
                    url=f"{BASE}/detail/{index}",
                    name=f"Coffee {index}",
                    payload="<html>feed</html>" if index == 1 else None,
                )

    fetcher, sink = FakeFetcher(), FakeSink()
    report = _run(MixedSite(), fetcher, sink)

    assert fetcher.requested == [f"{BASE}/detail/0", f"{BASE}/detail/2"]
    assert report.parsed == 3
    assert [coffee.external_id for coffee in sink.written] == ["0", "1", "2"]


def test_one_failing_product_does_not_abort_the_run() -> None:
    site = FakeSite(4, broken={"2"})
    sink = FakeSink()
    report = _run(site, FakeFetcher(), sink)

    assert report.parsed == 3
    assert report.failed == 1
    assert report.written == 3
    assert any("detail/2" in error for error in report.errors)
    assert report.complete is False or sink.delisted == []


def test_fetch_errors_are_counted_and_block_delisting() -> None:
    sink = FakeSink()
    fetcher = FakeFetcher({f"{BASE}/detail/1": "__error__"})
    report = _run(FakeSite(3), fetcher, sink)

    assert report.fetched == 2
    assert report.failed == 1
    assert report.complete is False
    assert sink.delisted == []
    assert "HTTP 500" in report.errors[0]


def test_disallowed_products_are_reported_separately() -> None:
    fetcher = FakeFetcher({f"{BASE}/detail/0": "__disallowed__"})
    report = _run(FakeSite(2), fetcher, FakeSink())

    assert report.disallowed == 1
    assert report.failed == 0
    assert report.complete is False


def test_non_coffee_products_are_skipped_not_failed() -> None:
    fetcher = FakeFetcher({f"{BASE}/detail/1": "not-coffee"})
    report = _run(FakeSite(3), fetcher, FakeSink())

    assert report.skipped_non_coffee == 1
    assert report.parsed == 2
    assert report.failed == 0


def test_ignored_names_are_skipped_before_parsing() -> None:
    class IgnoringSite(FakeSite):
        def ignored_names(self) -> tuple[str, ...]:
            return ("coffee 1",)

    report = _run(IgnoringSite(3), FakeFetcher(), FakeSink())
    assert report.skipped_non_coffee == 1
    assert report.parsed == 2


def test_products_are_written_in_batches() -> None:
    sink = FakeSink()
    _run(FakeSite(5), FakeFetcher(), sink, batch_size=2)
    assert sink.batches == [["0", "1"], ["2", "3"], ["4"]]


def test_discovery_failure_before_the_first_product_is_reported_not_raised() -> None:
    class ExplodingSite(FakeSite):
        def discover(
            self,
            fetcher: PoliteFetcher,
            *,
            max_pages: int | None = None,
        ) -> Iterator[ProductRef]:
            error = FetchError(f"{BASE}/list", "HTTP 503")
            raise error

    report = _run(ExplodingSite(), FakeFetcher(), FakeSink())
    assert report.failed == 1
    assert report.parsed == 0
    assert report.complete is False
    assert "discovery failed" in report.errors[0]


def test_a_listing_page_failing_mid_walk_keeps_what_was_already_found() -> None:
    class HalfBrokenSite(FakeSite):
        def discover(
            self,
            fetcher: PoliteFetcher,
            *,
            max_pages: int | None = None,
        ) -> Iterator[ProductRef]:
            yield ProductRef(
                site_id=self.site_id,
                external_id="0",
                url=f"{BASE}/detail/0",
                name="Coffee 0",
            )
            error = FetchError(f"{BASE}/list?page=2", "HTTP 503")
            raise error

    sink = FakeSink()
    report = _run(HalfBrokenSite(), FakeFetcher(), sink, batch_size=1)

    assert report.parsed == 1
    assert report.written == 1
    assert report.failed == 1
    assert report.complete is False
    assert sink.delisted == []


def test_error_list_is_capped() -> None:
    report = RunReport(site_id="fake")
    for index in range(MAX_REPORTED_ERRORS + 10):
        report.add_error(f"boom {index}")
    assert report.failed == MAX_REPORTED_ERRORS + 10
    assert len(report.errors) == MAX_REPORTED_ERRORS


@pytest.mark.parametrize("count", [0, 1])
def test_tiny_catalogues_do_not_crash(count: int) -> None:
    report = _run(FakeSite(count), FakeFetcher(), FakeSink())
    assert report.parsed == count


# --- price normalisation ------------------------------------------------------

RATE = FxRate(date=date(2026, 9, 11), rate=Decimal("24.26"), source="cnb")


def _eur_coffee() -> Coffee:
    coffee = make_coffee(site="sk", external_id="1")
    coffee.price = 9.99
    coffee.currency = "EUR"
    coffee.weight_g = 200
    coffee.variants = [Variant("1-250", None, 250, 11.5, "EUR", True, "250 g")]
    return coffee


def _czk_coffee() -> Coffee:
    coffee = make_coffee(site="cz", external_id="2")
    coffee.price = 287.0
    coffee.currency = "CZK"
    coffee.weight_g = 250
    coffee.variants = [Variant("2-1000", None, 1000, 999.0, None, True, "1 kg")]
    return coffee


def test_derive_fills_every_normalised_field_for_a_euro_shop() -> None:
    coffee = _eur_coffee()

    derive(coffee, RATE)

    assert coffee.price_eur == 9.99
    assert coffee.price_czk == 242.36
    assert coffee.price_per_kg == 49.95
    assert coffee.price_per_kg_eur == 49.95
    assert coffee.price_per_kg_czk == 1211.79
    assert coffee.fx_rate_eur_czk == 24.26
    assert coffee.fx_date == RATE.date
    assert (coffee.variants[0].price_eur, coffee.variants[0].price_czk) == (11.5, 278.99)


def test_derive_fills_every_normalised_field_for_a_crown_shop() -> None:
    coffee = _czk_coffee()

    derive(coffee, RATE)

    assert coffee.price_czk == 287.0
    assert coffee.price_eur == 11.83
    assert coffee.price_per_kg == 1148.0
    assert coffee.price_per_kg_czk == 1148.0
    assert coffee.price_per_kg_eur == 47.32
    # the variant states no currency of its own, so it inherits the product's
    assert (coffee.variants[0].price_czk, coffee.variants[0].price_eur) == (999.0, 41.18)


def test_derive_without_a_rate_leaves_everything_null() -> None:
    coffee = _eur_coffee()

    derive(coffee, None)

    record = coffee.to_record()
    for key in ("price_eur", "price_czk", "price_per_kg_eur", "price_per_kg_czk", "fx_date"):
        assert record[key] is None
    assert record["price_per_kg"] == 49.95
    assert coffee.variants[0].price_eur is None


def test_derive_leaves_an_unpriced_product_alone() -> None:
    coffee = make_coffee(external_id="3")
    coffee.price = None
    coffee.currency = None
    coffee.variants = []

    derive(coffee, RATE)

    assert coffee.price_eur is None
    assert coffee.price_czk is None
    # the fixing is still stamped, so a row states which rate it was written under
    assert coffee.fx_rate_eur_czk == 24.26


def test_a_crawl_stamps_the_rate_on_every_product() -> None:
    sink = FakeSink()
    report = _run(FakeSite(3), FakeFetcher(), sink, fx_rate=RATE)

    assert report.parsed == 3
    assert all(coffee.fx_date == RATE.date for coffee in sink.written)
    assert all(coffee.price_czk == 242.36 for coffee in sink.written)


def test_a_crawl_proceeds_when_no_rate_could_be_had() -> None:
    sink = FakeSink()
    report = _run(FakeSite(3), FakeFetcher(), sink, fx_rate=None)

    assert report.parsed == 3
    assert report.written == 3
    assert all(coffee.price_czk is None for coffee in sink.written)
    assert all(coffee.fx_rate_eur_czk is None for coffee in sink.written)


# --- several shops at once ----------------------------------------------------


def _run_many(
    shops: Sequence[FakeSite],
    fetcher: FakeFetcher,
    sink: FakeSink,
    *,
    site_workers: int,
) -> list[RunReport]:
    return run_many(
        cast("Sequence[SiteAdapter]", shops),
        cast("PoliteFetcher", fetcher),
        cast("Sink", sink),
        site_workers=site_workers,
    )


class SlowSite(FakeSite):
    """A shop that takes its time answering, like a real one."""

    def __init__(self, site_id: str, barrier: threading.Barrier) -> None:
        super().__init__(1)
        self.site_id = site_id
        self._barrier = barrier

    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        # Every shop must be inside discover at the same moment, or the barrier
        # times out: that is the proof they really do overlap.
        self._barrier.wait(timeout=5)
        yield from super().discover(fetcher, max_pages=max_pages)


def test_shops_are_crawled_at_the_same_time() -> None:
    barrier = threading.Barrier(3)
    shops = [SlowSite(f"shop{index}", barrier) for index in range(3)]
    fetcher, sink = FakeFetcher(), FakeSink()

    reports = _run_many(shops, fetcher, sink, site_workers=3)

    assert [report.site_id for report in reports] == ["shop0", "shop1", "shop2"]
    assert all(report.written == 1 for report in reports)


def test_one_shop_failing_does_not_stop_the_others() -> None:
    class Broken(FakeSite):
        site_id = "broken"

        def discover(
            self,
            fetcher: PoliteFetcher,
            *,
            max_pages: int | None = None,
        ) -> Iterator[ProductRef]:
            msg = "the shop moved its catalogue"
            raise RuntimeError(msg)

    shops = [FakeSite(2), Broken(0), FakeSite(2)]
    reports = _run_many(shops, FakeFetcher(), FakeSink(), site_workers=3)

    assert len(reports) == 3
    broken = next(report for report in reports if report.site_id == "broken")
    assert broken.complete is False
    assert any("RuntimeError" in error for error in broken.errors)
    assert [report.written for report in reports if report.site_id != "broken"] == [2, 2]


def test_a_single_worker_keeps_the_plain_sequential_path() -> None:
    shops = [FakeSite(1), FakeSite(1)]

    reports = _run_many(shops, FakeFetcher(), FakeSink(), site_workers=1)

    assert [report.written for report in reports] == [1, 1]


# --- the deadline -------------------------------------------------------------


class SlowDiscovery(FakeSite):
    """A shop whose listing walk costs a little time per product."""

    def __init__(self, count: int, step_s: float) -> None:
        super().__init__(count)
        self.step_s = step_s

    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        for ref in super().discover(fetcher, max_pages=max_pages):
            time.sleep(self.step_s)
            yield ref


def test_a_deadline_already_passed_stops_before_the_first_product() -> None:
    site, fetcher, sink = FakeSite(5), FakeFetcher(), FakeSink()

    report = _run(site, fetcher, sink, deadline=Deadline(at=time.monotonic() - 1))

    assert report.discovered == 0
    assert fetcher.requested == []
    assert report.deadline_reached is True
    assert report.complete is False


def test_a_truncated_run_never_delists() -> None:
    """The whole reason the deadline is reported rather than raised."""
    site, fetcher, sink = SlowDiscovery(20, 0.01), FakeFetcher(), FakeSink()

    report = _run(site, fetcher, sink, batch_size=2, deadline=Deadline.after(0.03))

    assert report.deadline_reached is True
    assert sink.delisted == []
    assert report.delisted == 0
    assert 0 < report.discovered < 20


def test_a_run_inside_its_deadline_is_complete_and_delists() -> None:
    site, fetcher, sink = FakeSite(3), FakeFetcher(), FakeSink()

    report = _run(site, fetcher, sink, deadline=Deadline.after(30))

    assert report.deadline_reached is False
    assert report.complete is True
    assert sink.delisted == [("fake", {"0", "1", "2"})]


def test_the_deadline_stops_between_batches_not_inside_one() -> None:
    site, fetcher, sink = SlowDiscovery(10, 0.01), FakeFetcher(), FakeSink()

    report = _run(site, fetcher, sink, batch_size=3, deadline=Deadline.after(0.045))

    # every product that was discovered was also fetched and written: a batch is
    # never abandoned half-way through
    assert report.written == report.discovered
    assert report.discovered % 3 == 0


def test_one_deadline_covers_every_shop_of_a_run() -> None:
    shops = [SlowDiscovery(10, 0.01) for _ in range(3)]
    for index, shop in enumerate(shops):
        shop.site_id = f"shop{index}"

    reports = run_many(
        cast("Sequence[SiteAdapter]", shops),
        cast("PoliteFetcher", FakeFetcher()),
        cast("Sink", FakeSink()),
        site_workers=1,
        batch_size=2,
        deadline=Deadline.after(0.03),
    )

    assert len(reports) == 3
    assert any(report.deadline_reached for report in reports)
    assert reports[-1].discovered < 10


def test_a_deadline_reached_report_is_recorded_as_incomplete() -> None:
    site, fetcher, sink = FakeSite(2), FakeFetcher(), FakeSink()
    report = _run(site, fetcher, sink, deadline=Deadline(at=time.monotonic() - 1))
    assert report.complete is False
    assert report.started_at is not None
    assert report.finished_at is not None
    assert report.finished_at >= report.started_at


# --- sharding -----------------------------------------------------------------


class Shop(FakeSite):
    """A registered shop at a given URL, for the shard arithmetic."""

    def __init__(self, site_id: str, base_url: str, max_pages: int = 4) -> None:
        super().__init__(0)
        self.site_id = site_id
        self.base_url = base_url
        self.max_pages = max_pages


def _flat(_host: str) -> float:
    return 1.0


def test_two_shops_on_one_host_never_land_in_different_shards() -> None:
    """Splitting an origin would have each shard pace it on its own."""
    shops = [
        Shop("a", "https://one.sk/"),
        Shop("b", "https://www.one.sk/"),
        Shop("c", "https://two.sk/"),
        Shop("d", "https://three.sk/"),
    ]

    picked = [
        {site.site_id for site in shard_sites(shops, index, 3, delay_for=_flat)}
        for index in (1, 2, 3)
    ]

    together = next(chosen for chosen in picked if "a" in chosen)
    assert "b" in together
    assert sorted(site_id for chosen in picked for site_id in chosen) == ["a", "b", "c", "d"]


def test_every_shop_lands_in_exactly_one_shard() -> None:
    shops = [Shop(f"s{index}", f"https://s{index}.sk/", max_pages=index + 1) for index in range(12)]

    seen = [
        site.site_id
        for index in (1, 2, 3, 4)
        for site in shard_sites(shops, index, 4, delay_for=_flat)
    ]

    assert sorted(seen) == sorted(site.site_id for site in shops)


def test_a_shard_selection_is_deterministic() -> None:
    shops = [Shop(f"s{index}", f"https://s{index}.sk/", max_pages=index + 1) for index in range(9)]

    first = [site.site_id for site in shard_sites(shops, 2, 3, delay_for=_flat)]
    second = [site.site_id for site in shard_sites(list(reversed(shops)), 2, 3, delay_for=_flat)]

    assert sorted(first) == sorted(second)


def test_shards_are_balanced_by_cost_and_not_by_count() -> None:
    """One 30-second crawl-delay shop outweighs a dozen fast ones."""
    slow = Shop("slow", "https://caffeoro.sk/", max_pages=5)
    fast = [Shop(f"f{index}", f"https://f{index}.sk/", max_pages=5) for index in range(6)]

    def delay_for(host: str) -> float:
        return 30.0 if host == "caffeoro.sk" else 1.0

    costs = host_costs([slow, *fast], delay_for=delay_for)
    shards = [shard_hosts(costs, index, 2) for index in (1, 2)]

    holding_slow = next(shard for shard in shards if any(c.host == "caffeoro.sk" for c in shard))
    assert [cost.host for cost in holding_slow] == ["caffeoro.sk"]
    assert len(shards[0]) + len(shards[1]) == 7


def test_the_cost_of_a_host_is_requests_times_its_delay() -> None:
    costs = host_costs([Shop("a", "https://a.sk/", max_pages=2)], delay_for=lambda _host: 3.0)

    assert len(costs) == 1
    assert costs[0].requests == 2 * (1 + PRODUCTS_PER_LISTING_PAGE)
    assert costs[0].seconds == costs[0].requests * 3.0


def test_max_pages_lowers_the_estimate_but_never_raises_it() -> None:
    shop = Shop("a", "https://a.sk/", max_pages=4)

    assert estimated_requests(shop, max_pages=1) == 1 + PRODUCTS_PER_LISTING_PAGE
    assert estimated_requests(shop, max_pages=99) == estimated_requests(shop)


@pytest.mark.parametrize(("index", "count"), [(0, 4), (5, 4), (1, 0), (-1, 2)])
def test_a_nonsense_shard_is_rejected(index: int, count: int) -> None:
    with pytest.raises(ShardError):
        shard_hosts([], index, count)


def test_one_shard_is_the_whole_list() -> None:
    shops = [Shop("a", "https://a.sk/"), Shop("b", "https://b.sk/")]
    assert [site.site_id for site in shard_sites(shops, 1, 1, delay_for=_flat)] == ["a", "b"]


def test_a_shard_with_more_shards_than_hosts_can_be_empty() -> None:
    shops = [Shop("a", "https://a.sk/")]
    assert shard_sites(shops, 2, 4, delay_for=_flat) == []


def test_a_shop_without_a_base_url_falls_back_to_its_id() -> None:
    assert host_of(Shop("lonely", "")) == "lonely"


# --- the run summary ----------------------------------------------------------


def test_wrote_nothing_names_only_the_empty_shops() -> None:
    reports = [
        RunReport(site_id="empty"),
        RunReport(site_id="full", written=3),
        RunReport(site_id="also-empty", written=0),
    ]
    assert wrote_nothing(reports) == ["also-empty", "empty"]
