from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from coffee_aggregator.db import migrate
from coffee_aggregator.fx.rates import FxRate
from coffee_aggregator.fx.stores import PostgresFxStore
from coffee_aggregator.models import ProcessMethod
from coffee_aggregator.pipeline import derive
from coffee_aggregator.sinks.postgres import COLUMNS, MANAGED_COLUMNS, PostgresSink
from conftest import make_coffee

if TYPE_CHECKING:
    from collections.abc import Iterator

FRIDAY = date(2026, 9, 11)
RATE = FxRate(date=FRIDAY, rate=Decimal("24.26"), source="cnb")
#: Every table the migrations own. The fixture drops exactly these, and only in
#: the database TEST_DATABASE_URL names — never in the development database.
TABLES = ("price_history", "coffee", "fx_rates", migrate.VERSION_TABLE)
_COUNT_VERSIONS_SQL = f"SELECT count(*) FROM {migrate.VERSION_TABLE}"  # noqa: S608  (a constant)

DSN = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests"),
]


def _drop_everything(postgres: PostgresSink) -> None:
    with postgres.connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
    postgres.connection.commit()


@pytest.fixture
def sink() -> Iterator[PostgresSink]:
    postgres = PostgresSink(DSN, chunk_size=1)
    _drop_everything(postgres)
    postgres.init_schema()
    yield postgres
    _drop_everything(postgres)
    postgres.close()


@pytest.fixture
def empty_database() -> Iterator[PostgresSink]:
    """A database with no tables at all, for the migration runner's own tests."""
    postgres = PostgresSink(DSN)
    _drop_everything(postgres)
    yield postgres
    _drop_everything(postgres)
    postgres.close()


def _columns_of(sink: PostgresSink, table: str) -> set[str]:
    with sink.connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
        names = {str(row[0]) for row in cursor.fetchall()}
    sink.connection.commit()
    return names


def _scalar(sink: PostgresSink, sql: str, params: tuple[object, ...] = ()) -> object:
    with sink.connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    sink.connection.commit()
    return None if row is None else row[0]


def test_the_runner_applies_every_migration_once(empty_database: PostgresSink) -> None:
    assert empty_database.pending_migrations() == migrate.versions()
    assert empty_database.init_schema() == ["0001_initial", "0002_fx_rates"]
    assert empty_database.init_schema() == []
    assert empty_database.pending_migrations() == []
    recorded = _scalar(empty_database, _COUNT_VERSIONS_SQL)
    assert recorded == len(migrate.versions())


def test_init_schema_is_idempotent(sink: PostgresSink) -> None:
    assert sink.init_schema() == []
    assert _scalar(sink, "SELECT count(*) FROM coffee") == 0


def test_the_live_columns_match_the_column_tuple(sink: PostgresSink) -> None:
    """The real table, not only the DDL text, must agree with COLUMNS."""
    assert _columns_of(sink, "coffee") == {*COLUMNS, *MANAGED_COLUMNS}
    assert {"price_eur", "price_per_kg_eur", "fx_date"} <= _columns_of(sink, "coffee")
    assert {"price_eur", "price_czk", "fx_rate_eur_czk"} <= _columns_of(sink, "price_history")
    assert _columns_of(sink, "fx_rates") == {
        "date",
        "base",
        "quote",
        "rate",
        "source",
        "fetched_at",
    }


def test_upsert_twice_is_idempotent_and_advances_last_seen(sink: PostgresSink) -> None:
    coffees = [make_coffee(external_id="1"), make_coffee(external_id="2")]

    first = sink.upsert(coffees)
    assert first.written == 2
    assert first.failed == 0
    first_seen = _scalar(sink, "SELECT last_seen_at FROM coffee WHERE external_id = '1'")

    coffees[0].price = 11.5
    second = sink.upsert(coffees)
    assert second.written == 2

    assert _scalar(sink, "SELECT count(*) FROM coffee") == 2
    assert _scalar(sink, "SELECT count(*) FROM price_history") == 4
    assert float(_scalar(sink, "SELECT price FROM coffee WHERE external_id = '1'")) == 11.5  # type: ignore[arg-type]
    assert _scalar(sink, "SELECT last_seen_at FROM coffee WHERE external_id = '1'") >= first_seen  # type: ignore[operator]


def test_json_and_typed_columns_survive_the_round_trip(sink: PostgresSink) -> None:
    sink.upsert([make_coffee(external_id="1")])

    assert _scalar(sink, "SELECT variety FROM coffee WHERE external_id = '1'") == [
        "Typica",
        "Bourbon",
    ]
    assert _scalar(sink, "SELECT raw_attributes->>'KRAJINA' FROM coffee") == "Kuba"
    assert _scalar(sink, "SELECT reviews->0->>'author' FROM coffee") == "Jana"
    assert str(_scalar(sink, "SELECT roast_date FROM coffee")) == "2026-09-01"
    assert _scalar(sink, "SELECT origin_country FROM coffee") == "CU"
    assert _scalar(sink, "SELECT process_method FROM coffee") == "washed"
    assert float(_scalar(sink, "SELECT price_per_kg FROM coffee")) == 49.95  # type: ignore[arg-type]


def test_mark_delisted_flags_exactly_the_missing_product(sink: PostgresSink) -> None:
    sink.upsert([make_coffee(external_id="1"), make_coffee(external_id="2")])

    assert sink.mark_delisted("demo", {"1"}) == 1
    assert _scalar(sink, "SELECT count(*) FROM coffee WHERE delisted_at IS NOT NULL") == 1
    assert _scalar(sink, "SELECT external_id FROM coffee WHERE delisted_at IS NOT NULL") == "2"
    assert sink.mark_delisted("demo", {"1"}) == 0


def test_a_returning_product_is_undelisted(sink: PostgresSink) -> None:
    sink.upsert([make_coffee(external_id="1")])
    # a non-empty set of ids the run did not see: an empty one is refused
    assert sink.mark_delisted("demo", {"999"}) == 1

    sink.upsert([make_coffee(external_id="1")])
    assert _scalar(sink, "SELECT delisted_at FROM coffee WHERE external_id = '1'") is None


def test_other_sites_are_never_delisted(sink: PostgresSink) -> None:
    sink.upsert(
        [make_coffee(site="demo", external_id="1"), make_coffee(site="other", external_id="1")]
    )

    assert sink.mark_delisted("demo", {"999"}) == 1
    survivors = _scalar(
        sink,
        "SELECT count(*) FROM coffee WHERE site = 'other' AND delisted_at IS NULL",
    )
    assert survivors == 1


def test_delisting_on_an_empty_set_is_refused(sink: PostgresSink) -> None:
    sink.upsert([make_coffee(external_id="1")])
    assert sink.mark_delisted("demo", set()) == 0
    assert _scalar(sink, "SELECT count(*) FROM coffee WHERE delisted_at IS NOT NULL") == 0


def test_process_methods_round_trips(sink: PostgresSink) -> None:
    coffee = make_coffee(external_id="1")
    coffee.processing.methods = [ProcessMethod.WASHED, ProcessMethod.NATURAL]
    coffee.processing.method = ProcessMethod.MIXED
    sink.upsert([coffee])

    assert _scalar(sink, "SELECT process_method FROM coffee") == "mixed"
    assert _scalar(sink, "SELECT process_methods FROM coffee") == ["washed", "natural"]


def test_the_normalised_prices_round_trip(sink: PostgresSink) -> None:
    coffee = make_coffee(external_id="1")
    coffee.price = 9.99
    coffee.currency = "EUR"
    coffee.weight_g = 200
    derive(coffee, RATE)

    sink.upsert([coffee])

    assert float(_scalar(sink, "SELECT price_czk FROM coffee")) == 242.36  # type: ignore[arg-type]
    assert float(_scalar(sink, "SELECT price_eur FROM coffee")) == 9.99  # type: ignore[arg-type]
    assert float(_scalar(sink, "SELECT price_per_kg_czk FROM coffee")) == 1211.79  # type: ignore[arg-type]
    assert str(_scalar(sink, "SELECT fx_date FROM coffee")) == "2026-09-11"
    assert float(_scalar(sink, "SELECT price_czk FROM price_history")) == 242.36  # type: ignore[arg-type]
    assert float(_scalar(sink, "SELECT fx_rate_eur_czk FROM price_history")) == 24.26  # type: ignore[arg-type]
    assert _scalar(sink, "SELECT variants->0->>'price_czk' FROM coffee") == "278.99"


def test_the_fx_store_round_trips_through_the_table(sink: PostgresSink) -> None:
    store = PostgresFxStore(DSN)
    try:
        assert store.get_latest() is None

        store.put(RATE)
        assert store.get(FRIDAY) == RATE
        assert store.get_latest() == RATE

        # a second fixing for the same day replaces the first, never duplicates it
        store.put(FxRate(date=FRIDAY, rate=Decimal("24.500"), source="ecb"))
        stored = store.get(FRIDAY)
        assert stored is not None
        assert (stored.rate, stored.source) == (Decimal("24.500"), "ecb")
        assert _scalar(sink, "SELECT count(*) FROM fx_rates") == 1

        older = FxRate(date=date(2026, 9, 4), rate=Decimal("24.100"), source="cnb")
        store.put(older)
        latest = store.get_latest()
        assert latest is not None
        assert latest.date == FRIDAY
    finally:
        store.close()


def test_the_price_per_kg_eur_index_exists(sink: PostgresSink) -> None:
    assert (
        _scalar(
            sink,
            "SELECT indexname FROM pg_indexes WHERE tablename = 'coffee' "
            "AND indexname = 'coffee_price_per_kg_eur_idx'",
        )
        == "coffee_price_per_kg_eur_idx"
    )
