from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Self

import pytest
from psycopg.types.json import Jsonb

from coffee_aggregator.db import migrate
from coffee_aggregator.sinks.postgres import (
    COLUMNS,
    DELIST_SQL,
    JSON_COLUMNS,
    MANAGED_COLUMNS,
    PRICE_HISTORY_COLUMNS,
    PRICE_HISTORY_SQL,
    UPSERT_SQL,
    VARIANT_COLUMNS,
    VARIANT_PRUNE_SQL,
    VARIANT_UPSERT_SQL,
    PostgresSink,
    SchemaOutOfDateError,
    row_for,
    schema_columns,
)
from conftest import make_coffee

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType


def _checksum_of(version: str) -> str:
    return next(m.checksum for m in migrate.load_migrations() if m.version == version)


class FakeCursor:
    """Records every statement instead of talking to a server."""

    def __init__(self, calls: list[tuple[str, str, Any]], applied: set[str] | None = None) -> None:
        self.calls = calls
        self.rowcount = 7
        self.applied = migrate.versions() if applied is None else sorted(applied)
        self._rows: list[tuple[str, str]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        self.calls.append(("execute", sql, params))
        if sql == migrate._SELECT_VERSIONS_SQL:
            self._rows = [(version, _checksum_of(version)) for version in self.applied]

    def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        self.calls.append(("executemany", sql, list(params_seq)))

    def fetchall(self) -> list[tuple[str, str]]:
        return self._rows

    def close(self) -> None:
        return None


class FakeConnection:
    """Just enough of psycopg.Connection for the sink."""

    def __init__(self, applied: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.applied = applied

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.calls, self.applied)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def sink() -> tuple[PostgresSink, FakeConnection]:
    connection = FakeConnection()
    postgres = PostgresSink("postgresql://fake/db", chunk_size=2)
    postgres._connection = connection  # type: ignore[assignment]
    # the schema gate has its own tests; here it would only add a SELECT
    postgres._schema_checked = True
    return postgres, connection


def test_upsert_sql_conflicts_on_the_primary_key() -> None:
    assert UPSERT_SQL.startswith("INSERT INTO coffee (")
    assert "ON CONFLICT (site, external_id) DO UPDATE SET" in UPSERT_SQL
    assert UPSERT_SQL.endswith("last_seen_at = now(), delisted_at = NULL")
    assert UPSERT_SQL.count("%s") == len(COLUMNS)


def test_upsert_sql_refreshes_every_data_column() -> None:
    for column in COLUMNS:
        if column in {"site", "external_id"}:
            assert f"{column} = EXCLUDED.{column}" not in UPSERT_SQL
        else:
            assert f"{column} = EXCLUDED.{column}" in UPSERT_SQL


def test_upsert_never_overwrites_the_managed_columns() -> None:
    for column in MANAGED_COLUMNS:
        assert f"{column} = EXCLUDED.{column}" not in UPSERT_SQL
    assert "first_seen_at" not in UPSERT_SQL


def test_upsert_chunks_and_writes_price_history(
    sink: tuple[PostgresSink, FakeConnection],
) -> None:
    postgres, connection = sink
    coffees = [make_coffee(external_id=str(index)) for index in range(5)]

    result = postgres.upsert(coffees)

    assert result.written == 5
    assert result.failed == 0
    upserts = [call for call in connection.calls if call[1] == UPSERT_SQL]
    prices = [call for call in connection.calls if call[1] == PRICE_HISTORY_SQL]
    variants = [call for call in connection.calls if call[1] == VARIANT_UPSERT_SQL]
    prunes = [call for call in connection.calls if call[1] == VARIANT_PRUNE_SQL]
    assert [len(call[2]) for call in upserts] == [2, 2, 1]
    assert [len(call[2]) for call in prices] == [2, 2, 1]
    assert [len(call[2]) for call in prunes] == [2, 2, 1]
    # one variant per coffee in the fixture
    assert [len(call[2]) for call in variants] == [2, 2, 1]
    assert all(call[0] == "executemany" for call in connection.calls if call[1] == UPSERT_SQL)
    assert connection.commits == 1


def test_price_history_row_carries_price_weight_and_availability(
    sink: tuple[PostgresSink, FakeConnection],
) -> None:
    postgres, connection = sink
    postgres.upsert([make_coffee(external_id="42")])
    price_call = next(call for call in connection.calls if call[1] == PRICE_HISTORY_SQL)
    assert price_call[2][0] == ("demo", "42", 9.99, "EUR", 200, True, None, None, None)


def test_json_columns_are_wrapped_in_jsonb() -> None:
    row = dict(zip(COLUMNS, row_for(make_coffee()), strict=True))
    for column in COLUMNS:
        if column in JSON_COLUMNS:
            assert isinstance(row[column], Jsonb), column
        else:
            assert not isinstance(row[column], Jsonb), column


def test_dates_stay_native_objects_for_psycopg() -> None:
    row = dict(zip(COLUMNS, row_for(make_coffee()), strict=True))
    assert not isinstance(row["roast_date"], str)
    assert not isinstance(row["scraped_at"], str)


def test_empty_batch_touches_nothing(sink: tuple[PostgresSink, FakeConnection]) -> None:
    postgres, connection = sink
    assert postgres.upsert([]).written == 0
    assert connection.calls == []


def test_mark_delisted_statement_and_parameters(
    sink: tuple[PostgresSink, FakeConnection],
) -> None:
    postgres, connection = sink
    count = postgres.mark_delisted("demo", {"1", "2"})

    assert count == 7
    call = connection.calls[-1]
    assert call[0] == "execute"
    assert call[1] == DELIST_SQL
    assert "delisted_at IS NULL" in DELIST_SQL
    assert "NOT (external_id = ANY(%s))" in DELIST_SQL
    assert call[2][0] == "demo"
    assert sorted(call[2][1]) == ["1", "2"]


def test_every_value_is_a_bound_parameter() -> None:
    for statement in (UPSERT_SQL, PRICE_HISTORY_SQL, DELIST_SQL):
        assert "'" not in statement
        assert re.search(r"%\(", statement) is None


def test_column_list_matches_every_migration() -> None:
    """The DDL and COLUMNS drift apart the moment one of them is edited alone."""
    assert set(schema_columns()) == {*COLUMNS, *MANAGED_COLUMNS}


def test_the_migrations_are_packaged_next_to_the_code() -> None:
    """``init-db`` must work from an installed wheel, not only from a checkout."""
    sql = "\n".join(migration.sql for migration in migrate.load_migrations())
    assert "CREATE TABLE IF NOT EXISTS coffee (" in sql
    assert "CREATE TABLE IF NOT EXISTS coffee_variant (" in sql
    assert "process_methods" in sql
    assert migrate.versions() == ["0001_initial"]


def test_one_migration_describes_the_whole_schema() -> None:
    """Five incremental files were collapsed once nothing had been deployed."""
    migrations = migrate.load_migrations()

    assert [item.version for item in migrations] == ["0001_initial"]
    for table in ("coffee", "coffee_variant", "price_history", "fx_rates"):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in migrations[0].sql
    for column in ("price_eur", "price_czk", "price_per_kg_eur", "fx_rate_eur_czk", "roaster"):
        assert column in migrations[0].sql


def test_price_history_columns_match_every_migration() -> None:
    """``seen_on`` is generated by the database, so the sink never writes it."""
    assert set(schema_columns("price_history")) == {
        "id",
        "seen_at",
        "seen_on",
        *PRICE_HISTORY_COLUMNS,
    }
    assert "seen_on" not in PRICE_HISTORY_COLUMNS


def test_variant_columns_match_the_migration() -> None:
    assert set(schema_columns("coffee_variant")) == {
        *VARIANT_COLUMNS,
        "first_seen_at",
        "last_seen_at",
    }


class ExplodingCursor(FakeCursor):
    """Fails on the first statement, the way a dropped connection does."""

    def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        message = "server closed the connection unexpectedly"
        raise RuntimeError(message)

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        message = "server closed the connection unexpectedly"
        raise RuntimeError(message)


class ExplodingConnection(FakeConnection):
    def cursor(self) -> FakeCursor:
        return ExplodingCursor(self.calls, self.applied)


def test_a_failed_upsert_rolls_back_and_then_retries_every_row_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing can be written here, so the retry only proves each row was tried."""
    connection = ExplodingConnection()
    postgres = PostgresSink("postgresql://fake/db")
    postgres._connection = connection  # type: ignore[assignment]
    postgres._schema_checked = True

    with caplog.at_level("ERROR", logger="coffee_aggregator.sinks.postgres"):
        result = postgres.upsert([make_coffee(external_id=str(index)) for index in range(3)])

    assert (result.written, result.failed) == (0, 3)
    # the batch, then one rollback per row of the retry
    assert connection.rollbacks == 4
    assert connection.commits == 0
    assert "rolling back" in caplog.text
    # the log names the product a human has to go and look at
    for external_id in ("0", "1", "2"):
        assert f"demo/{external_id}" in caplog.text


def test_one_bad_row_costs_only_itself(caplog: pytest.LogCaptureFixture) -> None:
    class OneBadRow(FakeConnection):
        """Refuses the batch, and then only the product whose row is malformed."""

        def cursor(self) -> FakeCursor:
            return PickyCursor(self.calls, self.applied)

    class PickyCursor(FakeCursor):
        def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
            rows = list(params_seq)
            if sql == UPSERT_SQL and any(row[1] == "bad" for row in rows):
                message = 'invalid input syntax for type date: "31.02.2026"'
                raise RuntimeError(message)
            super().executemany(sql, rows)

    connection = OneBadRow()
    postgres = PostgresSink("postgresql://fake/db")
    postgres._connection = connection  # type: ignore[assignment]
    postgres._schema_checked = True
    coffees = [make_coffee(external_id=external_id) for external_id in ("1", "bad", "2")]

    with caplog.at_level("ERROR", logger="coffee_aggregator.sinks.postgres"):
        result = postgres.upsert(coffees)

    assert (result.written, result.failed) == (2, 1)
    assert "demo/bad" in caplog.text
    assert "demo/1" not in caplog.text


def test_a_crawl_against_an_older_database_is_refused_before_it_writes() -> None:
    """A database that never ran the current schema must not be written to."""
    connection = FakeConnection(applied=set())
    postgres = PostgresSink("postgresql://fake/db")
    postgres._connection = connection  # type: ignore[assignment]

    with pytest.raises(SchemaOutOfDateError, match="0001_initial"):
        postgres.upsert([make_coffee()])

    assert not [call for call in connection.calls if call[1] == UPSERT_SQL]


def test_an_up_to_date_database_is_only_checked_once() -> None:
    connection = FakeConnection()
    postgres = PostgresSink("postgresql://fake/db")
    postgres._connection = connection  # type: ignore[assignment]

    postgres.upsert([make_coffee()])
    postgres.upsert([make_coffee()])

    selects = [call for call in connection.calls if call[1] == migrate._SELECT_VERSIONS_SQL]
    assert len(selects) == 1


def test_init_schema_spares_the_first_write_its_check() -> None:
    connection = FakeConnection()
    postgres = PostgresSink("postgresql://fake/db")
    postgres._connection = connection  # type: ignore[assignment]

    postgres.init_schema()
    before = len(connection.calls)
    postgres.upsert([make_coffee()])

    assert not [
        call for call in connection.calls[before:] if call[1] == migrate._SELECT_VERSIONS_SQL
    ]


def test_a_failed_delisting_rolls_back_and_returns_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = ExplodingConnection()
    postgres = PostgresSink("postgresql://fake/db")
    postgres._connection = connection  # type: ignore[assignment]
    postgres._schema_checked = True

    with caplog.at_level("ERROR", logger="coffee_aggregator.sinks.postgres"):
        assert postgres.mark_delisted("demo", {"1"}) == 0

    assert connection.rollbacks == 1
    assert "delisting for demo failed" in caplog.text


def test_delisting_on_an_empty_set_is_refused(
    sink: tuple[PostgresSink, FakeConnection],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty set means a broken crawl, not an empty shop."""
    postgres, connection = sink
    with caplog.at_level("WARNING", logger="coffee_aggregator.sinks.postgres"):
        assert postgres.mark_delisted("demo", set()) == 0

    assert connection.calls == []
    assert "refusing to delist" in caplog.text
