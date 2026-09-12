from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self

import pytest
import responses

from coffee_aggregator.fx import cnb, convert, ecb, rates, stores
from coffee_aggregator.fx.rates import FxRate, FxService
from coffee_aggregator.fx.stores import FileFxStore, PostgresFxStore
from coffee_aggregator.http import PoliteFetcher

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

CNB_ROBOTS = "https://www.cnb.cz/robots.txt"
ECB_ROBOTS = "https://www.ecb.europa.eu/robots.txt"

FRIDAY = date(2026, 9, 11)
SATURDAY = date(2026, 9, 12)
SUNDAY = date(2026, 9, 13)

CNB_TEXT = """11.09.2026 #176
země|měna|množství|kód|kurz
Austrálie|dolar|1|AUD|13,969
Brazílie|real|1|BRL|3,894
EMU|euro|1|EUR|24,260
Japonsko|jen|100|JPY|14,271
Maďarsko|forint|100|HUF|6,147
USA|dolar|1|USD|20,661
"""

ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
                 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <gesmes:subject>Reference rates</gesmes:subject>
  <Cube>
    <Cube time='2026-09-11'>
      <Cube currency='USD' rate='1.1742'/>
      <Cube currency='JPY' rate='172.55'/>
      <Cube currency='CZK' rate='24.264'/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""


def make_fetcher() -> PoliteFetcher:
    """A real PoliteFetcher with the waits turned down to nothing."""
    return PoliteFetcher(
        user_agent="coffee-aggregator/0.1.0",
        contact="https://example.org/contact",
        delay_s=0.0,
        jitter_s=0.0,
        workers=2,
        timeout_s=5.0,
        retries=1,
    )


def allow_robots() -> None:
    responses.add(responses.GET, CNB_ROBOTS, status=404)
    responses.add(responses.GET, ECB_ROBOTS, status=404)


class MemoryStore:
    """An FxStore that keeps everything in a dict, and counts what it is asked."""

    def __init__(self, seeded: Sequence[FxRate] = ()) -> None:
        self.rates: dict[date, FxRate] = {rate.date: rate for rate in seeded}
        self.puts: list[FxRate] = []

    def get(self, day: date) -> FxRate | None:
        return self.rates.get(day)

    def get_latest(self) -> FxRate | None:
        return max(self.rates.values(), key=lambda rate: rate.date) if self.rates else None

    def put(self, rate: FxRate) -> None:
        self.rates[rate.date] = rate
        self.puts.append(rate)


# --- CNB ---------------------------------------------------------------------


def test_cnb_reads_the_decimal_comma() -> None:
    rate = cnb.parse_rate(CNB_TEXT)
    assert rate is not None
    assert rate.rate == Decimal("24.260")
    assert rate.date == FRIDAY
    assert (rate.base, rate.quote, rate.source) == ("EUR", "CZK", "cnb")


def test_cnb_divides_the_currencies_quoted_per_hundred() -> None:
    fixing = cnb.parse_fixing(CNB_TEXT)
    assert fixing is not None
    assert fixing.rates["JPY"] == Decimal("0.14271")
    assert fixing.rates["HUF"] == Decimal("0.06147")
    assert fixing.rates["USD"] == Decimal("20.661")


def test_cnb_keeps_the_published_date_over_a_weekend() -> None:
    """Saturday's download carries Friday's fixing; that is the date recorded."""
    rate = cnb.parse_rate(CNB_TEXT)
    assert rate is not None
    assert rate.date == FRIDAY != SATURDAY


@pytest.mark.parametrize(
    "text",
    ["", "not a date at all\nzemě|měna|množství|kód|kurz\n", "11.09.2026 #176\n", "  \n\n"],
)
def test_cnb_returns_none_for_garbage(text: str) -> None:
    assert cnb.parse_rate(text) is None


def test_cnb_returns_none_when_the_euro_row_is_missing() -> None:
    text = "11.09.2026 #176\nzemě|měna|množství|kód|kurz\nUSA|dolar|1|USD|20,661\n"
    assert cnb.parse_rate(text) is None


def test_cnb_ignores_a_zero_amount_column() -> None:
    text = "11.09.2026 #176\nzemě|měna|množství|kód|kurz\nEMU|euro|0|EUR|24,260\n"
    assert cnb.parse_rate(text) is None


@responses.activate
def test_cnb_is_fetched_through_the_polite_fetcher() -> None:
    allow_robots()
    responses.add(responses.GET, cnb.CNB_URL, body=CNB_TEXT, content_type="text/plain")
    fetcher = make_fetcher()
    rate = cnb.fetch_rate(fetcher)
    fetcher.close()
    assert rate is not None
    assert rate.rate == Decimal("24.260")
    # robots.txt first, the fixing second: the bank is paced like any shop
    assert [call.request.url for call in responses.calls] == [CNB_ROBOTS, cnb.CNB_URL]


@responses.activate
def test_a_shop_robots_txt_that_forbids_us_is_obeyed_for_the_bank_too() -> None:
    responses.add(
        responses.GET,
        CNB_ROBOTS,
        body="User-agent: *\nDisallow: /\n",
        content_type="text/plain",
    )
    fetcher = make_fetcher()
    store = MemoryStore()
    rate = FxService(fetcher, store, sources=[cnb.fetch_rate]).get_rate(FRIDAY)
    fetcher.close()
    assert rate is None


# --- ECB ---------------------------------------------------------------------


def test_ecb_parses_the_reference_file() -> None:
    rate = ecb.parse_rate(ECB_XML)
    assert rate is not None
    assert rate.rate == Decimal("24.264")
    assert rate.date == FRIDAY
    assert (rate.base, rate.quote, rate.source) == ("EUR", "CZK", "ecb")


def test_ecb_returns_none_when_the_crown_is_absent() -> None:
    xml = ECB_XML.replace("<Cube currency='CZK' rate='24.264'/>", "")
    assert ecb.parse_rate(xml) is None


def test_ecb_returns_none_for_garbage() -> None:
    assert ecb.parse_rate("<html>down for maintenance</html>") is None


@responses.activate
def test_ecb_is_fetched_through_the_polite_fetcher() -> None:
    allow_robots()
    responses.add(responses.GET, ecb.ECB_URL, body=ECB_XML, content_type="text/xml")
    fetcher = make_fetcher()
    rate = ecb.fetch_rate(fetcher)
    fetcher.close()
    assert rate is not None
    assert rate.rate == Decimal("24.264")


# --- the service -------------------------------------------------------------


@responses.activate
def test_the_service_prefers_the_national_bank() -> None:
    allow_robots()
    responses.add(responses.GET, cnb.CNB_URL, body=CNB_TEXT, content_type="text/plain")
    responses.add(responses.GET, ecb.ECB_URL, body=ECB_XML, content_type="text/xml")
    fetcher = make_fetcher()
    store = MemoryStore()

    rate = FxService(fetcher, store).get_rate(FRIDAY)
    fetcher.close()

    assert rate is not None
    assert rate.source == "cnb"
    assert store.puts == [rate]
    assert ecb.ECB_URL not in [call.request.url for call in responses.calls]


@responses.activate
def test_the_service_falls_back_to_the_ecb() -> None:
    allow_robots()
    responses.add(responses.GET, cnb.CNB_URL, status=503)
    responses.add(responses.GET, ecb.ECB_URL, body=ECB_XML, content_type="text/xml")
    fetcher = make_fetcher()
    store = MemoryStore()

    rate = FxService(fetcher, store).get_rate(FRIDAY)
    fetcher.close()

    assert rate is not None
    assert rate.source == "ecb"
    assert rate.rate == Decimal("24.264")


@responses.activate
def test_the_service_falls_back_to_the_store_and_says_how_old_it_is(
    caplog: pytest.LogCaptureFixture,
) -> None:
    allow_robots()
    responses.add(responses.GET, cnb.CNB_URL, status=503)
    responses.add(responses.GET, ecb.ECB_URL, status=503)
    stale = FxRate(date=date(2026, 9, 4), rate=Decimal("24.100"), source="cnb")
    fetcher = make_fetcher()
    store = MemoryStore([stale])

    with caplog.at_level(logging.WARNING, logger="coffee_aggregator.fx.rates"):
        rate = FxService(fetcher, store).get_rate(FRIDAY)
    fetcher.close()

    assert rate == stale
    assert "7 day(s) old" in caplog.text


@responses.activate
def test_the_service_returns_none_when_the_store_is_empty_too() -> None:
    allow_robots()
    responses.add(responses.GET, cnb.CNB_URL, status=503)
    responses.add(responses.GET, ecb.ECB_URL, status=503)
    fetcher = make_fetcher()

    assert FxService(fetcher, MemoryStore()).get_rate(FRIDAY) is None
    fetcher.close()


@responses.activate
def test_the_rate_is_fetched_at_most_once_a_day() -> None:
    allow_robots()
    responses.add(responses.GET, cnb.CNB_URL, body=CNB_TEXT, content_type="text/plain")
    fetcher = make_fetcher()
    store = MemoryStore()
    service = FxService(fetcher, store)

    first = service.get_rate(FRIDAY)
    before = len(responses.calls)
    second = service.get_rate(FRIDAY)
    fetcher.close()

    assert first == second
    assert len(responses.calls) == before
    assert len(store.puts) == 1


@responses.activate
def test_a_weekend_reuses_fridays_fixing_without_a_request() -> None:
    """The bank publishes nothing on a Saturday, so nothing is worth asking for."""
    allow_robots()
    fetcher = make_fetcher()
    friday = FxRate(date=FRIDAY, rate=Decimal("24.260"), source="cnb")
    store = MemoryStore([friday])

    assert FxService(fetcher, store).get_rate(SATURDAY) == friday
    assert FxService(fetcher, store).get_rate(SUNDAY) == friday
    fetcher.close()
    assert len(responses.calls) == 0


@responses.activate
def test_refresh_ignores_the_store() -> None:
    allow_robots()
    responses.add(responses.GET, cnb.CNB_URL, body=CNB_TEXT, content_type="text/plain")
    fetcher = make_fetcher()
    store = MemoryStore([FxRate(date=FRIDAY, rate=Decimal("1.000"), source="stale")])

    rate = FxService(fetcher, store).get_rate(FRIDAY, refresh=True)
    fetcher.close()

    assert rate is not None
    assert rate.rate == Decimal("24.260")
    assert store.puts


def test_a_store_that_raises_never_reaches_the_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenStore:
        def get(self, day: date) -> FxRate | None:
            message = "no such table"
            raise RuntimeError(message)

        def get_latest(self) -> FxRate | None:
            message = "no such table"
            raise RuntimeError(message)

        def put(self, rate: FxRate) -> None:
            message = "no such table"
            raise RuntimeError(message)

    fetcher = make_fetcher()
    fixed = FxRate(date=FRIDAY, rate=Decimal("24.260"), source="fake")
    service = FxService(fetcher, BrokenStore(), sources=[lambda _fetcher: fixed])

    with caplog.at_level(logging.WARNING, logger="coffee_aggregator.fx.rates"):
        assert service.get_rate(FRIDAY) == fixed
    fetcher.close()
    assert "could not" in caplog.text


def test_latest_expected_fixing_rolls_the_weekend_back() -> None:
    assert rates.latest_expected_fixing(SATURDAY) == FRIDAY
    assert rates.latest_expected_fixing(SUNDAY) == FRIDAY
    assert rates.latest_expected_fixing(FRIDAY) == FRIDAY


# --- stores ------------------------------------------------------------------


def test_file_store_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "fx_rates.json"
    store = FileFxStore(path)
    assert store.get(FRIDAY) is None
    assert store.get_latest() is None

    rate = FxRate(date=FRIDAY, rate=Decimal("24.260"), source="cnb")
    store.put(rate)

    assert path.is_file()
    assert FileFxStore(path).get(FRIDAY) == rate
    assert FileFxStore(path).get_latest() == rate


def test_file_store_keeps_several_days_and_returns_the_newest(tmp_path: Path) -> None:
    store = FileFxStore(tmp_path / "fx.json")
    older = FxRate(date=date(2026, 9, 4), rate=Decimal("24.100"), source="cnb")
    newer = FxRate(date=FRIDAY, rate=Decimal("24.260"), source="cnb")
    store.put(newer)
    store.put(older)

    assert store.get_latest() == newer
    assert store.get(older.date) == older


def test_file_store_survives_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "fx.json"
    path.write_text("{not json", encoding="utf-8")
    store = FileFxStore(path)
    assert store.get_latest() is None
    store.put(FxRate(date=FRIDAY, rate=Decimal("24.260"), source="cnb"))
    assert store.get_latest() is not None


def test_the_default_cache_path_sits_under_the_http_cache(tmp_path: Path) -> None:
    assert stores.default_cache_path(tmp_path) == tmp_path / "fx_rates.json"
    assert stores.default_cache_path().name == "fx_rates.json"


class FakeCursor:
    """Records statements and returns one canned row."""

    def __init__(self, calls: list[tuple[str, Any]], row: tuple[Any, ...] | None) -> None:
        self.calls = calls
        self.row = row

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:  # noqa: ANN401
        self.calls.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class FakeConnection:
    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.row = row
        self.closed = False
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.calls, self.row)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _postgres_store(row: tuple[Any, ...] | None = None) -> tuple[PostgresFxStore, FakeConnection]:
    connection = FakeConnection(row)
    store = PostgresFxStore("postgresql://fake/db")
    store._connection = connection  # type: ignore[assignment]
    return store, connection


def test_postgres_store_upserts_on_the_primary_key() -> None:
    store, connection = _postgres_store()
    rate = FxRate(date=FRIDAY, rate=Decimal("24.260"), source="cnb")

    store.put(rate)

    sql, params = connection.calls[-1]
    assert sql == stores.UPSERT_SQL
    assert "ON CONFLICT (date, base, quote) DO UPDATE SET" in sql
    assert "fetched_at = now()" in sql
    assert params == (FRIDAY, "EUR", "CZK", Decimal("24.260"), "cnb")
    assert connection.commits == 1


def test_postgres_store_reads_one_day_and_the_latest() -> None:
    store, connection = _postgres_store((FRIDAY, Decimal("24.260"), "cnb"))

    assert store.get(FRIDAY) == FxRate(date=FRIDAY, rate=Decimal("24.260"), source="cnb")
    sql, params = connection.calls[-1]
    assert (sql, params) == (stores.SELECT_ONE_SQL, (FRIDAY, "EUR", "CZK"))

    assert store.get_latest() is not None
    sql, params = connection.calls[-1]
    assert (sql, params) == (stores.SELECT_LATEST_SQL, ("EUR", "CZK"))
    assert "ORDER BY date DESC LIMIT 1" in stores.SELECT_LATEST_SQL


def test_postgres_store_returns_none_for_an_empty_table() -> None:
    store, _ = _postgres_store(None)
    assert store.get(FRIDAY) is None
    assert store.get_latest() is None


def test_postgres_store_sql_binds_every_value() -> None:
    for sql in (stores.SELECT_ONE_SQL, stores.SELECT_LATEST_SQL, stores.UPSERT_SQL):
        assert "'" not in sql


def test_a_missing_fx_table_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    class ExplodingConnection(FakeConnection):
        def cursor(self) -> FakeCursor:
            message = 'relation "fx_rates" does not exist'
            raise RuntimeError(message)

    store = PostgresFxStore("postgresql://fake/db")
    store._connection = ExplodingConnection()  # type: ignore[assignment]

    with caplog.at_level(logging.ERROR, logger="coffee_aggregator.fx.stores"):
        assert store.get_latest() is None
        store.put(FxRate(date=FRIDAY, rate=Decimal("24.260"), source="cnb"))
    assert "run init-db" in caplog.text


# --- conversion --------------------------------------------------------------

RATE = FxRate(date=FRIDAY, rate=Decimal("24.26"), source="cnb")


def test_euro_prices_become_crowns() -> None:
    assert convert.to_czk(9.99, "EUR", RATE) == 242.36
    assert convert.to_eur(9.99, "EUR", RATE) == 9.99


def test_crown_prices_become_euros() -> None:
    assert convert.to_eur(287.0, "CZK", RATE) == 11.83
    assert convert.to_czk(287.0, "CZK", RATE) == 287.0


@pytest.mark.parametrize("currency", ["USD", "gbp", "", None])
def test_an_unknown_currency_converts_to_nothing(currency: str | None) -> None:
    assert convert.to_eur(10.0, currency, RATE) is None
    assert convert.to_czk(10.0, currency, RATE) is None


def test_a_missing_amount_converts_to_nothing() -> None:
    assert convert.to_eur(None, "CZK", RATE) is None
    assert convert.to_czk(None, "EUR", RATE) is None


def test_the_currency_code_may_be_written_any_way() -> None:
    assert convert.to_czk(1.0, " eur ", RATE) == 24.26
    assert convert.to_eur(24.26, "czk", RATE) == 1.0


def test_a_nonsense_rate_converts_to_nothing() -> None:
    broken = FxRate(date=FRIDAY, rate=Decimal(0), source="fake")
    assert convert.to_eur(287.0, "CZK", broken) is None
    assert convert.to_czk(9.99, "EUR", broken) is None


def test_conversion_rounds_to_the_cent_half_up() -> None:
    rate = FxRate(date=FRIDAY, rate=Decimal(2), source="fake")
    assert convert.to_czk(1.125, "EUR", rate) == 2.25
    assert convert.to_eur(1.005, "CZK", rate) == 0.5
