from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from coffee_aggregator.db import migrate
from coffee_aggregator.fx.rates import FxRate
from coffee_aggregator.fx.stores import PostgresFxStore
from coffee_aggregator.models import ProcessMethod, Variant
from coffee_aggregator.pipeline import derive
from coffee_aggregator.sinks.postgres import (
    COLUMNS,
    MANAGED_COLUMNS,
    PostgresSink,
    SchemaOutOfDateError,
)
from conftest import make_coffee

if TYPE_CHECKING:
    from collections.abc import Iterator

FRIDAY = date(2026, 9, 11)
RATE = FxRate(date=FRIDAY, rate=Decimal("24.26"), source="cnb")
#: Every table the migrations own. The fixture drops exactly these, and only in
#: the database TEST_DATABASE_URL names — never in the development database.
#: Dropped in this order: the variants reference the products.
TABLES = (
    "coffee_variant",
    "price_history",
    "crawl_run",
    "coffee",
    "fx_rates",
    migrate.VERSION_TABLE,
)
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
    assert empty_database.init_schema() == migrate.versions()
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
    # two crawls on one day are two rows, not four: a Lambda retry must not
    # write the day twice
    assert _scalar(sink, "SELECT count(*) FROM price_history") == 2
    assert float(_scalar(sink, "SELECT price FROM price_history WHERE external_id = '1'")) == 11.5  # type: ignore[arg-type]
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


def test_the_variants_are_rows_a_query_can_reach(sink: PostgresSink) -> None:
    coffee = make_coffee(external_id="1")
    derive(coffee, RATE)
    sink.upsert([coffee])

    assert _scalar(sink, "SELECT count(*) FROM coffee_variant") == 1
    assert _scalar(sink, "SELECT variant_key FROM coffee_variant") == "1-250"
    assert _scalar(sink, "SELECT weight_g FROM coffee_variant") == 250
    assert float(_scalar(sink, "SELECT price_eur FROM coffee_variant")) == 11.5  # type: ignore[arg-type]
    assert float(_scalar(sink, "SELECT price_czk FROM coffee_variant")) == 278.99  # type: ignore[arg-type]
    assert float(_scalar(sink, "SELECT price_per_kg_eur FROM coffee_variant")) == 46.0  # type: ignore[arg-type]
    assert _scalar(sink, "SELECT available FROM coffee_variant") is True


def test_the_cheapest_250_g_bag_is_one_plain_query(sink: PostgresSink) -> None:
    """The question the whole project exists to answer."""
    cheap = make_coffee(site="cz", external_id="1")
    cheap.variants = [Variant(external_id="a", weight_g=250, price=250.0, currency="CZK")]
    dear = make_coffee(site="sk", external_id="1")
    dear.variants = [Variant(external_id="a", weight_g=250, price=20.0, currency="EUR")]
    for coffee in (cheap, dear):
        derive(coffee, RATE)
    sink.upsert([cheap, dear])

    assert (
        _scalar(
            sink,
            "SELECT site FROM coffee_variant WHERE weight_g = 250 AND price_eur IS NOT NULL "
            "ORDER BY price_eur LIMIT 1",
        )
        == "cz"
    )


def test_a_dropped_package_stops_being_on_sale(sink: PostgresSink) -> None:
    coffee = make_coffee(external_id="1")
    coffee.variants = [
        Variant(external_id="a", weight_g=250, price=11.5, currency="EUR"),
        Variant(external_id="b", weight_g=1000, price=40.0, currency="EUR"),
    ]
    sink.upsert([coffee])
    assert _scalar(sink, "SELECT count(*) FROM coffee_variant") == 2

    coffee.variants = coffee.variants[:1]
    sink.upsert([coffee])

    assert _scalar(sink, "SELECT count(*) FROM coffee_variant") == 1
    assert _scalar(sink, "SELECT variant_key FROM coffee_variant") == "a"


def test_a_deleted_product_takes_its_variants_with_it(sink: PostgresSink) -> None:
    sink.upsert([make_coffee(external_id="1")])
    with sink.connection.cursor() as cursor:
        cursor.execute("DELETE FROM coffee WHERE external_id = '1'")
    sink.connection.commit()

    assert _scalar(sink, "SELECT count(*) FROM coffee_variant") == 0


def test_the_roaster_lands_in_its_own_column(sink: PostgresSink) -> None:
    czech = make_coffee(site="cz", external_id="1", raw_attributes={"BRAND": "Pražírna Mlýnek"})
    slovak = make_coffee(site="sk", external_id="1", raw_attributes={"BRAND": "PRAZIRNA MLYNEK"})
    sink.upsert([czech, slovak])

    assert _scalar(sink, "SELECT roaster FROM coffee WHERE site = 'cz'") == "Pražírna Mlýnek"
    assert (
        _scalar(
            sink,
            "SELECT count(DISTINCT site) FROM coffee WHERE roaster_key = 'prazirna mlynek'",
        )
        == 2
    )


def test_a_retried_invocation_refreshes_the_day_instead_of_doubling_it(
    sink: PostgresSink,
) -> None:
    coffee = make_coffee(external_id="1")
    sink.upsert([coffee])
    coffee.price = 12.5
    sink.upsert([coffee])
    sink.upsert([coffee])

    assert _scalar(sink, "SELECT count(*) FROM price_history") == 1
    assert float(_scalar(sink, "SELECT price FROM price_history")) == 12.5  # type: ignore[arg-type]
    assert _scalar(sink, "SELECT seen_on = (now() AT TIME ZONE 'UTC')::date FROM price_history")


def test_the_price_history_indexes_exist(sink: PostgresSink) -> None:
    indexes = {
        "price_history_day_idx": "UNIQUE",
        "price_history_seen_at_brin_idx": "brin",
    }
    for name, expected in indexes.items():
        definition = _scalar(
            sink,
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'price_history' AND indexname = %s",
            (name,),
        )
        assert definition is not None, name
        assert expected in str(definition), name


def test_a_crawl_against_an_older_database_is_refused(sink: PostgresSink) -> None:
    """A Lambda shipped ahead of its database must say so, not fail 157 rows."""
    newest = migrate.versions()[-1]
    with sink.connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {migrate.VERSION_TABLE} WHERE version = %s",  # noqa: S608
            (newest,),
        )
    sink.connection.commit()
    sink._schema_checked = False

    with pytest.raises(SchemaOutOfDateError, match=newest):
        sink.upsert([make_coffee()])

    assert _scalar(sink, "SELECT count(*) FROM coffee") == 0


def test_two_runners_never_migrate_at_the_same_time(empty_database: PostgresSink) -> None:
    """The second one waits for the lock, then finds nothing left to do."""
    other = PostgresSink(DSN)
    try:
        with other.connection.cursor() as cursor:
            cursor.execute(migrate._LOCK_SQL, (migrate.LOCK_KEY,))
        blocked = _scalar(
            empty_database,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s",
            (migrate.LOCK_KEY,),
        )
        assert blocked == 1
        with other.connection.cursor() as cursor:
            cursor.execute(migrate._UNLOCK_SQL, (migrate.LOCK_KEY,))
        other.connection.commit()
    finally:
        other.close()

    assert empty_database.init_schema() == migrate.versions()


def test_an_edited_migration_is_refused(sink: PostgresSink) -> None:
    with sink.connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {migrate.VERSION_TABLE} SET checksum = 'edited' WHERE version = %s",  # noqa: S608
            ("0001_initial",),
        )
    sink.connection.commit()

    with pytest.raises(migrate.MigrationChecksumError, match="0001_initial"):
        sink.pending_migrations()


def test_the_connection_is_not_pinned_by_a_prepared_statement(sink: PostgresSink) -> None:
    """A server-prepared statement is what stops RDS Proxy multiplexing."""
    for _ in range(10):
        sink.upsert([make_coffee(external_id="1")])

    assert _scalar(sink, "SELECT count(*) FROM pg_prepared_statements") == 0
    assert _scalar(sink, "SHOW statement_timeout") == "1min"
