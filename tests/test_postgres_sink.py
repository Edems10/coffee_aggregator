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
    PostgresSink,
    row_for,
    schema_columns,
)
from conftest import make_coffee

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType


class FakeCursor:
    """Records every statement instead of talking to a server."""

    def __init__(self, calls: list[tuple[str, str, Any]]) -> None:
        self.calls = calls
        self.rowcount = 7

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

    def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        self.calls.append(("executemany", sql, list(params_seq)))


class FakeConnection:
    """Just enough of psycopg.Connection for the sink."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.calls)

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
    assert [len(call[2]) for call in upserts] == [2, 2, 1]
    assert [len(call[2]) for call in prices] == [2, 2, 1]
    assert all(call[0] == "executemany" for call in connection.calls)
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
    assert "process_methods" in sql
    assert migrate.versions() == ["0001_initial", "0002_fx_rates"]


def test_the_fx_columns_arrive_in_the_second_migration() -> None:
    first, second = migrate.load_migrations()
    assert "price_per_kg_eur" not in first.sql
    for column in ("price_eur", "price_czk", "price_per_kg_eur", "fx_rate_eur_czk", "fx_date"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in second.sql
    assert "CREATE TABLE IF NOT EXISTS fx_rates (" in second.sql


def test_price_history_columns_match_every_migration() -> None:
    assert set(schema_columns("price_history")) == {"id", "seen_at", *PRICE_HISTORY_COLUMNS}


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
        return ExplodingCursor(self.calls)


def test_a_failed_upsert_rolls_back_and_reports_every_row_as_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = ExplodingConnection()
    postgres = PostgresSink("postgresql://fake/db")
    postgres._connection = connection  # type: ignore[assignment]

    with caplog.at_level("ERROR", logger="coffee_aggregator.sinks.postgres"):
        result = postgres.upsert([make_coffee(external_id=str(index)) for index in range(3)])

    assert (result.written, result.failed) == (0, 3)
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert "rolling back" in caplog.text


def test_a_failed_delisting_rolls_back_and_returns_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = ExplodingConnection()
    postgres = PostgresSink("postgresql://fake/db")
    postgres._connection = connection  # type: ignore[assignment]

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
