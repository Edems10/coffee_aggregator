from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self

from coffee_aggregator.db.connect import Connection
from coffee_aggregator.db.report import (
    DARK_RUN_SHARE,
    HIGH,
    LOW,
    MIN_DARK_SHOPS,
    NEW_FAILURES,
    NO_PRODUCTS,
    PARSE_GAP,
    PRICE_JUMP,
    ROLLUP_PRODUCTS,
    WEIGHT_CHANGE,
    WRITE_DROP,
    Finding,
    findings,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

DAY = date(2026, 10, 2)
YESTERDAY = date(2026, 10, 1)


def run(  # noqa: PLR0913  (one keyword per column; a dict would read worse)
    site: str,
    day: date,
    *,
    written: int = 10,
    discovered: int | None = None,
    fetched: int | None = None,
    parsed: int | None = None,
    skipped: int = 0,
    failed: int = 0,
    disallowed: int = 0,
    complete: bool = True,
    discovery_ok: bool = True,
    deadline: bool = False,
    errors: Sequence[str] = (),
    hour: int = 3,
) -> tuple[Any, ...]:
    """One crawl_run row, in the order the module's SELECT asks for."""
    fetched = written if fetched is None else fetched
    parsed = fetched if parsed is None else parsed
    discovered = fetched + skipped if discovered is None else discovered
    return (
        site,
        datetime(day.year, day.month, day.day, hour, tzinfo=UTC),
        discovered,
        fetched,
        parsed,
        skipped,
        failed,
        disallowed,
        written,
        0,
        complete,
        discovery_ok,
        deadline,
        list(errors),
    )


def price(  # noqa: PLR0913  (same)
    site: str,
    external_id: str,
    day: date,
    amount: str | None,
    weight_g: int | None = 250,
    *,
    name: str = "A Coffee",
    currency: str = "CZK",
) -> tuple[Any, ...]:
    """One price_history row joined to its name, in the SELECT's order."""
    return (
        site,
        external_id,
        day,
        None if amount is None else Decimal(amount),
        currency,
        weight_g,
        name,
    )


class FakeCursor:
    """Replays two lists of tuples, honouring the window each query asks for."""

    def __init__(self, runs: Sequence[tuple[Any, ...]], prices: Sequence[tuple[Any, ...]]) -> None:
        self.runs = runs
        self.prices = prices
        self.rows: list[tuple[Any, ...]] = []
        self.queries: list[str] = []

    def execute(self, query: str, params: Sequence[Any] | None = None) -> None:
        self.queries.append(query)
        assert params is not None
        first, last = params
        if "crawl_run" in query:
            self.rows = [row for row in self.runs if first <= row[1] < last]
        else:
            self.rows = [row for row in self.prices if first <= row[2] <= last]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        runs: Sequence[tuple[Any, ...]] = (),
        prices: Sequence[tuple[Any, ...]] = (),
    ) -> None:
        self.runs = runs
        self.prices = prices
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self.runs, self.prices)
        self.cursors.append(cursor)
        return cursor


def report(
    runs: Sequence[tuple[Any, ...]] = (),
    prices: Sequence[tuple[Any, ...]] = (),
    *,
    day: date = DAY,
    history_days: int = 7,
) -> list[Finding]:
    return findings(FakeConnection(runs, prices), day=day, history_days=history_days)


def kinds(found: Sequence[Finding]) -> list[str]:
    return [finding.kind for finding in found]


def only(found: Sequence[Finding], kind: str) -> list[Finding]:
    return [finding for finding in found if finding.kind == kind]


def steady(site: str, written: int, days: int = 3) -> list[tuple[Any, ...]]:
    """The same shop writing the same number every day up to and including DAY."""
    return [run(site, DAY - timedelta(days=offset), written=written) for offset in range(days)]


# --- the contract -------------------------------------------------------------


def test_the_fake_connection_satisfies_the_protocol() -> None:
    assert isinstance(FakeConnection(), Connection)


def test_a_steady_catalogue_is_worth_no_findings() -> None:
    runs = [row for site in ("alpha", "beta", "gamma") for row in steady(site, 12)]
    prices = [
        price(site, "1", day, "300")
        for site in ("alpha", "beta", "gamma")
        for day in (YESTERDAY, DAY)
    ]

    assert report(runs, prices) == []


def test_every_detail_is_json_native() -> None:
    runs = [
        *steady("alpha", 40)[1:],
        run("alpha", DAY, written=2, failed=4, errors=["fetch failed for https://x/1: HTTP 500"]),
        run("beta", YESTERDAY, written=5),
        run("beta", DAY, written=0, discovered=0, discovery_ok=False),
    ]
    prices = [
        price("alpha", "1", YESTERDAY, "300", 250),
        price("alpha", "1", DAY, "600", 500),
        price("alpha", "2", YESTERDAY, "300", 250),
        price("alpha", "2", DAY, "450", 250),
    ]

    found = report(runs, prices)

    assert found
    for finding in found:
        assert json.loads(json.dumps(finding.detail)) == finding.detail


def test_findings_are_sorted_by_severity_then_site() -> None:
    runs = [
        *steady("zeta", 12)[1:],
        run("zeta", DAY, written=0, discovered=0, discovery_ok=False),
    ]
    prices = [price("alpha", "1", YESTERDAY, "100"), price("alpha", "1", DAY, "200")]

    found = report(runs, prices)

    assert [(finding.severity, finding.site) for finding in found] == [
        (HIGH, "zeta"),
        (LOW, "alpha"),
    ]


def test_a_catalogue_wide_finding_sorts_above_the_named_shops() -> None:
    runs = [run(f"shop{index}", DAY, written=0, discovered=0) for index in range(MIN_DARK_SHOPS)]

    found = report(runs)

    assert [finding.site for finding in found] == [""]


def test_the_two_queries_read_only_the_window_asked_for() -> None:
    connection = FakeConnection(steady("alpha", 10, days=9))

    findings(connection, day=DAY, history_days=2)

    assert len(connection.cursors) == 2
    assert all("SELECT" in cursor.queries[0] for cursor in connection.cursors)
    assert len(connection.cursors[0].rows) == 3


# --- no-products --------------------------------------------------------------


def test_a_shop_that_wrote_nothing_is_reported_with_its_first_error() -> None:
    runs = [
        run("alpha", YESTERDAY, written=12),
        run(
            "alpha",
            DAY,
            written=0,
            discovered=0,
            discovery_ok=False,
            complete=False,
            failed=1,
            errors=["discovery failed for alpha: HTTP 404", "second"],
        ),
    ]

    (finding,) = report(runs)

    assert finding.kind == NO_PRODUCTS
    assert finding.severity == HIGH
    assert "HTTP 404" in finding.summary
    assert finding.detail["error"] == "discovery failed for alpha: HTTP 404"


def test_a_shop_that_went_quiet_carries_what_it_last_wrote() -> None:
    runs = [
        run("alpha", DAY - timedelta(days=2), written=4),
        run("alpha", DAY, written=0, discovered=0),
    ]

    (finding,) = report(runs)

    assert finding.detail["last_written"] == 4
    assert finding.detail["last_written_on"] == (DAY - timedelta(days=2)).isoformat()
    assert "last wrote 4" in finding.summary


def test_a_shop_whose_products_were_all_filtered_says_so() -> None:
    runs = [run("alpha", DAY, written=0, discovered=41, fetched=0, skipped=41)]

    (finding,) = report(runs)

    assert "skipped as non-coffee" in finding.summary


def test_many_empty_shops_become_one_finding_about_the_run() -> None:
    dark = [run(f"dark{index}", DAY, written=0, discovered=0, deadline=True) for index in range(79)]
    alive = [run(f"live{index}", DAY, written=12) for index in range(78)]

    found = report([*dark, *alive])

    assert kinds(found) == [NO_PRODUCTS]
    assert found[0].site == ""
    assert found[0].detail == {
        "shops": 157,
        "dark": 79,
        "deadline_reached": 79,
        "sites": sorted(f"dark{index}" for index in range(79)),
    }
    assert "79 of 157" in found[0].summary


def test_a_few_empty_shops_stay_one_finding_each() -> None:
    dark = [
        run(f"dark{index}", DAY, written=0, discovered=0) for index in range(MIN_DARK_SHOPS - 1)
    ]
    alive = [run(f"live{index}", DAY, written=12) for index in range(50)]

    found = report([*dark, *alive])

    assert len(found) == MIN_DARK_SHOPS - 1
    assert {finding.site for finding in found} == {f"dark{index}" for index in range(4)}


def test_one_shop_crawled_alone_is_never_a_catalogue_wide_finding() -> None:
    found = report([run("alpha", DAY, written=0, discovered=0)])

    # One of one shop is 100% of the run; only the floor keeps this readable.
    assert [finding.site for finding in found] == ["alpha"]
    assert DARK_RUN_SHARE < 1.0


# --- write-drop ---------------------------------------------------------------


def test_a_shop_that_lost_most_of_its_catalogue_is_reported() -> None:
    runs = [
        run("kavakromeriz", DAY - timedelta(days=2), written=126),
        run("kavakromeriz", YESTERDAY, written=126),
        run("kavakromeriz", DAY, written=4),
    ]

    (finding,) = report(runs)

    assert finding.kind == WRITE_DROP
    assert finding.severity == HIGH
    assert finding.detail["median_written"] == 126
    assert finding.detail["ratio"] == 0.032
    assert "wrote 4" in finding.summary


def test_a_small_shop_is_not_broken_for_being_small() -> None:
    assert report(steady("mlkcoffee", 4)) == []


def test_a_small_shop_halving_is_below_the_baseline_a_ratio_needs() -> None:
    runs = [
        run("mlkcoffee", DAY - timedelta(days=2), written=4),
        run("mlkcoffee", YESTERDAY, written=4),
        run("mlkcoffee", DAY, written=1),
    ]

    assert report(runs) == []


def test_half_the_usual_count_is_a_drop_and_more_than_half_is_not() -> None:
    history = [run("alpha", DAY - timedelta(days=offset), written=14) for offset in (1, 2)]

    assert kinds(report([*history, run("alpha", DAY, written=7)])) == [WRITE_DROP]
    assert report([*history, run("alpha", DAY, written=8)]) == []


def test_a_run_cut_off_by_its_deadline_is_not_blamed_for_writing_less() -> None:
    history = [run("alpha", DAY - timedelta(days=offset), written=40) for offset in (1, 2)]
    today = run("alpha", DAY, written=10, deadline=True, complete=False)

    assert report([*history, today]) == []


def test_a_truncated_day_is_not_used_as_a_baseline() -> None:
    runs = [
        run("alpha", DAY - timedelta(days=2), written=60),
        run("alpha", YESTERDAY, written=2, deadline=True, complete=False),
        run("alpha", DAY, written=55),
    ]

    # The median of [60, 2] would be 31 and 55 would look healthy against it;
    # the median of the one full day is 60, and 55 is still healthy.
    assert report(runs) == []
    assert kinds(report([*runs[:2], run("alpha", DAY, written=20)])) == [WRITE_DROP]


def test_a_shop_with_no_usable_history_is_left_alone() -> None:
    runs = [
        run("alpha", YESTERDAY, written=0, discovered=0, deadline=True, discovery_ok=False),
        run("alpha", DAY, written=3),
    ]

    assert report(runs) == []


def test_history_reaches_no_further_than_asked() -> None:
    runs = [
        run("alpha", DAY - timedelta(days=5), written=100),
        run("alpha", DAY, written=10),
    ]

    assert report(runs, history_days=7)[0].kind == WRITE_DROP
    assert report(runs, history_days=2) == []


# --- parse-gap ----------------------------------------------------------------


def test_pages_that_came_back_and_became_nothing_are_a_parse_gap() -> None:
    runs = [run("alpha", DAY, written=4, fetched=40, parsed=4, discovered=40)]

    (finding,) = report(runs)

    assert finding.kind == PARSE_GAP
    assert finding.severity == HIGH
    assert finding.detail["lost"] == 36
    assert "fetched 40 pages and wrote 4" in finding.summary


def test_a_shop_that_filters_most_of_its_listing_is_not_a_parse_gap() -> None:
    # theminers: 41 discovered, 27 of them merchandise, 14 fetched, 14 written.
    runs = [
        run("theminers", DAY - timedelta(days=offset), written=14, skipped=27)
        for offset in range(3)
    ]

    assert report(runs) == []


def test_one_rotten_page_on_a_large_shop_is_not_a_parse_gap() -> None:
    runs = [
        run("alpha", DAY - timedelta(days=offset), written=499, fetched=500, parsed=499)
        for offset in range(3)
    ]

    assert report(runs) == []


def test_a_parse_gap_replaces_the_write_drop_it_causes() -> None:
    runs = [run("alpha", DAY - timedelta(days=offset), written=40) for offset in (1, 2)]
    runs.append(run("alpha", DAY, written=4, fetched=40, parsed=4, discovered=40))

    assert kinds(report(runs)) == [PARSE_GAP]


# --- new-failures -------------------------------------------------------------


def test_an_error_that_was_not_there_before_is_reported() -> None:
    runs = [
        run("grinders", YESTERDAY, written=14),
        run(
            "grinders",
            DAY,
            written=12,
            fetched=12,
            discovered=17,
            failed=5,
            discovery_ok=False,
            complete=False,
            errors=["discovery failed for grinders: HTTP 500"],
        ),
    ]

    found = only(report(runs), NEW_FAILURES)

    assert len(found) == 1
    assert found[0].severity == HIGH
    assert found[0].detail["new_errors"] == ["discovery failed for grinders: HTTP 500"]


def test_the_same_error_two_nights_running_is_not_news() -> None:
    error = "fetch failed for https://x/1: HTTP 500"
    runs = [
        run("alpha", YESTERDAY, written=20, fetched=20, discovered=21, failed=1, errors=[error]),
        run("alpha", DAY, written=20, fetched=20, discovered=21, failed=1, errors=[error]),
    ]

    assert report(runs) == []


def test_a_sharp_rise_in_failures_is_news_even_with_a_familiar_error() -> None:
    error = "fetch failed for https://x/1: HTTP 500"
    runs = [
        run(
            "alpha",
            DAY - timedelta(days=offset),
            written=100,
            discovered=101,
            failed=1,
            errors=[error],
        )
        for offset in (1, 2)
    ]
    runs.append(run("alpha", DAY, written=100, discovered=112, failed=12, errors=[error]))

    found = only(report(runs), NEW_FAILURES)

    assert len(found) == 1
    assert found[0].detail["failures"] == 12
    assert found[0].severity == HIGH


def test_a_handful_of_failures_on_a_healthy_shop_is_low() -> None:
    runs = [
        run("alpha", YESTERDAY, written=200),
        run("alpha", DAY, written=200, discovered=203, failed=3, errors=["fetch failed: HTTP 500"]),
    ]

    found = only(report(runs), NEW_FAILURES)

    assert [finding.severity for finding in found] == [LOW]


def test_a_dark_shops_failures_are_not_reported_twice() -> None:
    runs = [
        run("alpha", YESTERDAY, written=12),
        run(
            "alpha",
            DAY,
            written=0,
            discovered=0,
            failed=1,
            discovery_ok=False,
            errors=["discovery failed for alpha: HTTP 404"],
        ),
    ]

    assert kinds(report(runs)) == [NO_PRODUCTS]


def test_one_closed_category_is_not_a_failure_worth_reporting() -> None:
    runs = [
        run("alpha", DAY - timedelta(days=offset), written=30, discovered=31, disallowed=1)
        for offset in range(3)
    ]

    assert report(runs) == []


# --- weight-change ------------------------------------------------------------


def test_a_variant_flip_is_a_weight_change_and_not_a_price_jump() -> None:
    runs = steady("nordbeans", 23)
    prices = [
        price("nordbeans", "4008", YESTERDAY, "375", 250, name="La Orquidea Honey"),
        price("nordbeans", "4008", DAY, "1363", 1000, name="La Orquidea Honey"),
    ]

    (finding,) = report(runs, prices)

    assert finding.kind == WEIGHT_CHANGE
    assert finding.site == "nordbeans"
    assert round(finding.detail["price_move"], 3) == 2.635
    assert round(finding.detail["per_kg_move"], 3) == -0.091
    assert "375 CZK/250 g -> 1363 CZK/1000 g" in finding.summary


def test_a_weight_that_stopped_being_parsed_is_the_serious_kind() -> None:
    runs = steady("frolikovakava", 12)
    prices = [
        price("frolikovakava", "2595", YESTERDAY, "259", 250),
        price("frolikovakava", "2595", DAY, "259", None),
    ]

    (finding,) = report(runs, prices)

    assert finding.kind == WEIGHT_CHANGE
    assert finding.severity == HIGH
    assert "unknown weight" in finding.summary


def test_an_isolated_variant_flip_is_only_worth_a_low() -> None:
    runs = steady("nordbeans", 23)
    prices = [
        price("nordbeans", "1", YESTERDAY, "375", 250),
        price("nordbeans", "1", DAY, "1363", 1000),
    ]

    assert report(runs, prices)[0].severity == LOW


def test_a_shop_whose_weights_all_moved_at_once_is_one_finding() -> None:
    runs = steady("alpha", 20)
    prices = [
        row
        for index in range(ROLLUP_PRODUCTS)
        for row in (
            price("alpha", str(index), YESTERDAY, "300", 250),
            price("alpha", str(index), DAY, "300", None),
        )
    ]

    (finding,) = report(runs, prices)

    assert finding.kind == WEIGHT_CHANGE
    assert finding.severity == HIGH
    assert len(finding.detail["products"]) == ROLLUP_PRODUCTS
    assert "3 products changed weight" in finding.summary


# --- price-jump ---------------------------------------------------------------


def test_a_price_that_moved_far_enough_is_reported_per_kilogram() -> None:
    runs = steady("caffe4u", 38)
    prices = [
        price("caffe4u", "3947", YESTERDAY, "10", 250, name="VECERNA SOVA", currency="EUR"),
        price("caffe4u", "3947", DAY, "8", 250, name="VECERNA SOVA", currency="EUR"),
    ]

    (finding,) = report(runs, prices)

    assert finding.kind == PRICE_JUMP
    assert finding.severity == LOW
    assert round(finding.detail["move"], 3) == -0.2
    assert "-20.0%" in finding.summary


def test_an_ordinary_price_move_is_left_out() -> None:
    runs = steady("alpha", 12)
    prices = [
        price("alpha", "1", YESTERDAY, "100"),
        price("alpha", "1", DAY, "112"),
    ]

    assert report(runs, prices) == []


def test_a_shop_wide_repricing_is_one_finding_not_thirteen() -> None:
    runs = steady("lighthousecoffee", 20)
    prices = [
        row
        for index in range(13)
        for row in (
            price("lighthousecoffee", str(index), YESTERDAY, "7.56", 200, currency="EUR"),
            price("lighthousecoffee", str(index), DAY, "9.00", 200, currency="EUR"),
        )
    ]

    (finding,) = report(runs, prices)

    assert finding.kind == PRICE_JUMP
    assert finding.detail["up"] == 13
    assert finding.detail["down"] == 0
    assert "13 products moved" in finding.summary


def test_a_product_with_no_price_yesterday_cannot_have_jumped() -> None:
    runs = steady("alpha", 12)
    prices = [
        price("alpha", "1", YESTERDAY, None),
        price("alpha", "1", DAY, "500"),
    ]

    assert report(runs, prices) == []


def test_a_product_seen_only_today_is_not_compared() -> None:
    runs = steady("alpha", 12)

    assert report(runs, [price("alpha", "new", DAY, "500")]) == []


def test_the_comparison_reaches_back_past_a_night_a_shop_missed() -> None:
    runs = steady("alpha", 12)
    prices = [
        price("alpha", "1", DAY - timedelta(days=3), "100"),
        price("alpha", "1", DAY, "200"),
    ]

    (finding,) = report(runs, prices)

    assert finding.kind == PRICE_JUMP
    assert finding.detail["previous_day"] == (DAY - timedelta(days=3)).isoformat()


def test_the_price_of_a_pack_that_changed_size_is_judged_per_kilogram() -> None:
    """A per-kilogram move inside the threshold is no jump, whatever the shelf says."""
    runs = steady("alpha", 12)
    prices = [
        price("alpha", "1", YESTERDAY, "375", 250),
        price("alpha", "1", DAY, "1363", 1000),
    ]

    assert kinds(report(runs, prices)) == [WEIGHT_CHANGE]


# --- folding several runs of one day -----------------------------------------


def test_two_runs_of_one_shop_on_one_day_fold_to_the_better_one() -> None:
    runs = [
        run("kafista", YESTERDAY, written=75),
        run("kafista", DAY, written=36, complete=False, deadline=True, hour=1),
        run("kafista", DAY, written=75, hour=2),
    ]

    assert report(runs) == []


def test_a_days_errors_are_pooled_across_its_runs() -> None:
    runs = [
        run("alpha", YESTERDAY, written=30),
        run("alpha", DAY, written=30, discovered=31, failed=1, errors=["first"], hour=1),
        run("alpha", DAY, written=30, discovered=31, failed=1, errors=["second"], hour=2),
    ]

    (finding,) = only(report(runs), NEW_FAILURES)

    assert finding.detail["new_errors"] == ["first", "second"]


def test_a_crawl_is_placed_on_the_utc_day_it_started() -> None:
    """``price_history.seen_on`` is generated in UTC; the runs must agree with it."""
    late: tuple[Any, ...] = (
        "alpha",
        datetime(2026, 10, 2, 23, 30, tzinfo=UTC),
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        True,
        True,
        False,
        [],
    )

    assert kinds(report([run("alpha", YESTERDAY, written=12), late])) == [NO_PRODUCTS]
    assert report([run("alpha", YESTERDAY, written=12), late], day=YESTERDAY) == []
