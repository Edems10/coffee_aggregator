from __future__ import annotations

from typing import Any, cast

import pytest

from coffee_aggregator.db import migrate


def _checksum_of(version: str) -> str:
    return next(m.checksum for m in migrate.load_migrations() if m.version == version)


class FakeCursor:
    """Records statements and hands back whatever versions the test wants."""

    def __init__(self, calls: list[tuple[str, Any]], recorded: dict[str, str | None]) -> None:
        self.calls = calls
        self.recorded = recorded
        self._rows: list[tuple[str, str | None]] = []

    def execute(self, sql: str, params: Any = None) -> None:  # noqa: ANN401
        self.calls.append((sql, params))
        if sql == migrate._SELECT_VERSIONS_SQL:
            self._rows = [(version, self.recorded[version]) for version in sorted(self.recorded)]
        elif sql == migrate._INSERT_VERSION_SQL and params:
            self.recorded[str(params[0])] = str(params[1])
        elif sql == migrate._BACKFILL_CHECKSUM_SQL and params:
            self.recorded[str(params[1])] = str(params[0])

    def fetchall(self) -> list[tuple[str, str | None]]:
        return self._rows

    def close(self) -> None:
        return None


class FakeConnection:
    """Just enough of a DB-API connection for the runner."""

    def __init__(self, recorded: dict[str, str | None] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.recorded: dict[str, str | None] = {} if recorded is None else recorded
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.calls, self.recorded)

    def rollback(self) -> None:
        self.calls.append(("ROLLBACK", None))

    def commit(self) -> None:
        self.commits += 1


def _applied_state(*versions: str) -> dict[str, str | None]:
    """The bookkeeping rows of a database that already ran these migrations."""
    return {version: _checksum_of(version) for version in versions}


def _connection(recorded: dict[str, str | None] | None = None) -> Any:  # noqa: ANN401
    return cast("Any", FakeConnection(recorded))


def test_every_packaged_migration_is_listed_in_lexical_order() -> None:
    # The file names are the order, so they have to sort into it. Pinning the
    # list itself would mean editing this test for every schema change, which
    # is how a test stops meaning anything.
    assert migrate.versions() == sorted(migrate.versions())
    assert migrate.versions()[0] == "0001_initial"


def test_an_empty_database_has_every_migration_pending() -> None:
    assert migrate.pending(_connection()) == migrate.versions()


def test_applying_records_each_version_and_is_then_a_no_op() -> None:
    connection = _connection()

    assert migrate.apply_migrations(connection) == migrate.versions()
    assert migrate.apply_migrations(connection) == []
    assert migrate.pending(connection) == []


def test_each_migration_commits_together_with_its_version_row() -> None:
    connection = FakeConnection()
    migrate.apply_migrations(cast("Any", connection))

    statements = [sql for sql, _ in connection.calls]
    inserts = [index for index, sql in enumerate(statements) if sql == migrate._INSERT_VERSION_SQL]
    assert len(inserts) == len(migrate.versions())
    # the DDL always comes immediately before the row that records it
    for index in inserts:
        assert any(
            keyword in statements[index - 1]
            for keyword in ("CREATE TABLE", "ALTER TABLE", "DELETE")
        )
    # one commit for the bookkeeping table, then one per migration
    assert connection.commits == len(migrate.versions()) + 1


def test_a_database_already_holding_the_schema_gets_nothing() -> None:
    """There is one migration today; a second one must still only run once."""
    connection = _connection(_applied_state(*migrate.versions()))

    assert migrate.pending(connection) == []
    assert migrate.apply_migrations(connection) == []


def test_the_lock_is_taken_before_the_version_table_is_even_created() -> None:
    connection = FakeConnection()
    migrate.apply_migrations(cast("Any", connection))
    assert connection.calls[0] == (migrate._LOCK_SQL, (migrate.LOCK_KEY,))
    assert connection.calls[1][0] == migrate.CREATE_VERSION_TABLE_SQL
    assert "schema_migrations" in migrate.CREATE_VERSION_TABLE_SQL


def test_the_lock_is_session_level_and_always_released() -> None:
    """The runner commits between files, so a transaction-level lock would drop."""
    assert "pg_advisory_lock" in migrate._LOCK_SQL
    assert "xact" not in migrate._LOCK_SQL
    connection = FakeConnection()
    migrate.apply_migrations(cast("Any", connection))

    # The rollback is what makes the release possible when a migration aborted;
    # on a clean run it is a no-op and the unlock still comes last.
    assert connection.calls[-2] == ("ROLLBACK", None)
    assert connection.calls[-1] == (migrate._UNLOCK_SQL, (migrate.LOCK_KEY,))


def test_a_dry_run_reports_the_pending_versions_and_writes_nothing() -> None:
    connection = FakeConnection()

    assert migrate.apply_migrations(cast("Any", connection), dry_run=True) == migrate.versions()

    assert migrate._INSERT_VERSION_SQL not in [sql for sql, _ in connection.calls]
    assert migrate._LOCK_SQL not in [sql for sql, _ in connection.calls]
    assert migrate.pending(cast("Any", connection)) == migrate.versions()


def test_an_edited_migration_is_refused_instead_of_silently_skipped() -> None:
    connection = _connection({"0001_initial": "cafe" * 16})

    with pytest.raises(migrate.MigrationChecksumError, match="0001_initial"):
        migrate.pending(connection)

    with pytest.raises(migrate.MigrationChecksumError):
        migrate.apply_migrations(connection)


def test_a_database_from_before_checksums_gets_them_backfilled() -> None:
    connection = FakeConnection({"0001_initial": None})

    migrate.apply_migrations(cast("Any", connection))

    assert connection.recorded["0001_initial"] == _checksum_of("0001_initial")
    assert migrate.pending(cast("Any", connection)) == []


def test_a_checksum_is_the_fingerprint_of_the_file_itself() -> None:
    first = migrate.load_migrations()[0]
    other = migrate.Migration(version=first.version, sql=first.sql + "\n-- edited\n")

    assert first.checksum == migrate.Migration(version=first.version, sql=first.sql).checksum
    assert first.checksum != other.checksum
    assert len(first.checksum) == 64


def test_versions_are_bound_parameters_not_formatted_into_the_sql() -> None:
    assert migrate._INSERT_VERSION_SQL.endswith("VALUES (%s, %s)")
    assert "'" not in migrate._INSERT_VERSION_SQL


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("coffee", "price_per_kg_eur"),
        ("coffee", "roaster_key"),
        ("coffee_variant", "price_per_kg_eur"),
        ("price_history", "fx_rate_eur_czk"),
        ("price_history", "seen_on"),
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
