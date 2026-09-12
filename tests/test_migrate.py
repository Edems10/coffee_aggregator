from __future__ import annotations

from typing import Any, cast

import pytest

from coffee_aggregator.db import migrate


class FakeCursor:
    """Records statements and hands back whatever versions the test wants."""

    def __init__(self, calls: list[tuple[str, Any]], recorded: set[str]) -> None:
        self.calls = calls
        self.recorded = recorded
        self._rows: list[tuple[str]] = []

    def execute(self, sql: str, params: Any = None) -> None:  # noqa: ANN401
        self.calls.append((sql, params))
        if sql == migrate._SELECT_VERSIONS_SQL:
            self._rows = [(version,) for version in sorted(self.recorded)]
        elif sql == migrate._INSERT_VERSION_SQL and params:
            self.recorded.add(str(params[0]))

    def fetchall(self) -> list[tuple[str]]:
        return self._rows

    def close(self) -> None:
        return None


class FakeConnection:
    """Just enough of a DB-API connection for the runner."""

    def __init__(self, recorded: set[str] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.recorded = recorded or set()
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.calls, self.recorded)

    def commit(self) -> None:
        self.commits += 1


def _connection(recorded: set[str] | None = None) -> Any:  # noqa: ANN401
    return cast("Any", FakeConnection(recorded))


def test_every_packaged_migration_is_listed_in_lexical_order() -> None:
    assert migrate.versions() == sorted(migrate.versions())
    assert migrate.versions() == ["0001_initial", "0002_fx_rates"]


def test_an_empty_database_has_every_migration_pending() -> None:
    assert migrate.pending(_connection()) == migrate.versions()


def test_applying_records_each_version_and_is_then_a_no_op() -> None:
    connection = _connection()

    assert migrate.apply_migrations(connection) == ["0001_initial", "0002_fx_rates"]
    assert migrate.apply_migrations(connection) == []
    assert migrate.pending(connection) == []


def test_each_migration_commits_together_with_its_version_row() -> None:
    connection = FakeConnection()
    migrate.apply_migrations(cast("Any", connection))

    statements = [sql for sql, _ in connection.calls]
    inserts = [index for index, sql in enumerate(statements) if sql == migrate._INSERT_VERSION_SQL]
    assert len(inserts) == 2
    # the DDL always comes immediately before the row that records it
    for index in inserts:
        assert "CREATE TABLE" in statements[index - 1] or "ALTER TABLE" in statements[index - 1]
    # one commit for the bookkeeping table, then one per migration
    assert connection.commits == 3


def test_a_half_applied_database_only_gets_the_rest() -> None:
    connection = _connection({"0001_initial"})
    assert migrate.pending(connection) == ["0002_fx_rates"]
    assert migrate.apply_migrations(connection) == ["0002_fx_rates"]


def test_the_version_table_is_created_before_anything_else() -> None:
    connection = FakeConnection()
    migrate.apply_migrations(cast("Any", connection))
    assert connection.calls[0][0] == migrate.CREATE_VERSION_TABLE_SQL
    assert "schema_migrations" in migrate.CREATE_VERSION_TABLE_SQL


def test_versions_are_bound_parameters_not_formatted_into_the_sql() -> None:
    assert migrate._INSERT_VERSION_SQL.endswith("VALUES (%s)")
    assert "'" not in migrate._INSERT_VERSION_SQL


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("coffee", "price_per_kg_eur"),
        ("price_history", "fx_rate_eur_czk"),
        ("fx_rates", "source"),
    ],
)
def test_table_columns_replays_creates_and_alters(table: str, expected: str) -> None:
    columns = migrate.table_columns(table)
    assert expected in columns
    assert len(columns) == len(set(columns))


def test_table_columns_never_mixes_two_tables_up() -> None:
    assert "site" not in migrate.table_columns("fx_rates")
    assert "url" not in migrate.table_columns("price_history")
    assert "rate" not in migrate.table_columns("coffee")


def test_table_columns_of_an_unknown_table_is_empty() -> None:
    assert migrate.table_columns("nope") == []
