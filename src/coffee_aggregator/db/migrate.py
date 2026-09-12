from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: Where the ``.sql`` files live, read through ``importlib.resources`` so the
#: runner works from an installed wheel exactly as it does from a checkout.
MIGRATIONS_PACKAGE = "coffee_aggregator.db"
MIGRATIONS_DIRECTORY = "migrations"
MIGRATION_SUFFIX = ".sql"

#: The bookkeeping table. It is the only thing the runner creates outside a
#: migration, and it is created before the first version is applied.
VERSION_TABLE = "schema_migrations"
CREATE_VERSION_TABLE_SQL = (
    f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} ("
    "version text PRIMARY KEY, "
    "applied_at timestamptz NOT NULL DEFAULT now())"
)
_SELECT_VERSIONS_SQL = f"SELECT version FROM {VERSION_TABLE}"  # noqa: S608  (same)
_INSERT_VERSION_SQL = f"INSERT INTO {VERSION_TABLE} (version) VALUES (%s)"  # noqa: S608  (same)

#: ``CREATE TABLE IF NOT EXISTS <name> (`` — the opening of a table definition.
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<table>\w+)\s*\(",
    re.IGNORECASE,
)
#: ``ALTER TABLE <name> ADD COLUMN IF NOT EXISTS <column> <type>;``
_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(?P<table>\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<column>\w+)\b",
    re.IGNORECASE,
)
#: Lines inside a ``CREATE TABLE`` body that declare a constraint, not a column.
_CONSTRAINT_PREFIXES = ("--", "PRIMARY KEY", "UNIQUE", "CONSTRAINT", "FOREIGN KEY", "CHECK")


class Cursor(Protocol):
    """The little of a DB-API cursor the runner needs."""

    def execute(self, query: str, params: Sequence[Any] | None = None, /) -> Any:  # noqa: ANN401  (drivers return themselves)
        """Run one statement.

        Args:
            query: The SQL to run.
            params: Bound parameters, when the statement takes any.

        Returns:
            Whatever the driver returns; the runner ignores it.
        """

    def fetchall(self) -> Sequence[Sequence[Any]]:
        """Return every row of the last statement.

        Returns:
            The rows, each a sequence of column values.
        """

    def close(self) -> None:
        """Release the cursor."""


class Connection(Protocol):
    """The little of a DB-API connection the runner needs."""

    def cursor(self) -> Cursor:
        """Open a cursor.

        Returns:
            A cursor on this connection.
        """

    def commit(self) -> None:
        """Commit the open transaction."""


@dataclass(slots=True, frozen=True)
class Migration:
    """One versioned ``.sql`` file."""

    version: str
    sql: str


def load_migrations() -> list[Migration]:
    """Read every packaged migration, in the order they must be applied.

    Returns:
        The migrations sorted by version, which is their file name without the
        ``.sql`` suffix — zero-padded, so lexical order is numeric order.
    """
    directory = resources.files(MIGRATIONS_PACKAGE).joinpath(MIGRATIONS_DIRECTORY)
    migrations = [
        Migration(version=entry.name.removesuffix(MIGRATION_SUFFIX), sql=entry.read_text("utf-8"))
        for entry in directory.iterdir()
        if entry.is_file() and entry.name.endswith(MIGRATION_SUFFIX)
    ]
    return sorted(migrations, key=lambda migration: migration.version)


def versions() -> list[str]:
    """Return every packaged version, in order.

    Returns:
        The version strings, e.g. ``["0001_initial", "0002_fx_rates"]``.
    """
    return [migration.version for migration in load_migrations()]


def _applied(connection: Connection) -> set[str]:
    """Return the versions the database already records.

    Args:
        connection: An open connection; the version table is created if missing.

    Returns:
        Every recorded version.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(CREATE_VERSION_TABLE_SQL)
        cursor.execute(_SELECT_VERSIONS_SQL)
        rows = cursor.fetchall()
    finally:
        cursor.close()
    connection.commit()
    return {str(row[0]) for row in rows}


def pending(connection: Connection) -> list[str]:
    """Return the versions that still have to be applied, in order.

    Args:
        connection: An open connection.

    Returns:
        The versions not yet recorded in :data:`VERSION_TABLE`.
    """
    done = _applied(connection)
    return [migration.version for migration in load_migrations() if migration.version not in done]


def apply_migrations(connection: Connection) -> list[str]:
    """Apply every pending migration, each in its own transaction.

    A migration and the row that records it are committed together, so a crash
    can never leave a version half applied or applied twice. Re-running is a
    no-op and returns an empty list.

    Args:
        connection: An open connection with autocommit disabled.

    Returns:
        The versions applied by this call, in order.
    """
    done = _applied(connection)
    applied: list[str] = []
    for migration in load_migrations():
        if migration.version in done:
            continue
        logger.info("applying migration %s", migration.version)
        cursor = connection.cursor()
        try:
            cursor.execute(migration.sql)
            cursor.execute(_INSERT_VERSION_SQL, (migration.version,))
        finally:
            cursor.close()
        connection.commit()
        applied.append(migration.version)
    if not applied:
        logger.debug("no pending migrations")
    return applied


def table_columns(table: str) -> list[str]:
    """Replay every migration on paper and return a table's resulting columns.

    This is what keeps the DDL and :data:`~coffee_aggregator.sinks.postgres.COLUMNS`
    from drifting without a database to ask: the ``CREATE TABLE`` of one migration
    and the ``ADD COLUMN`` of the next are read in the same order the runner would
    apply them.

    Args:
        table: The table to collect columns for, e.g. ``"coffee"``.

    Returns:
        Its column names in the order the migrations introduce them.
    """
    columns: list[str] = []
    for migration in load_migrations():
        for name in _created_columns(migration.sql, table):
            if name not in columns:
                columns.append(name)
        for match in _ADD_COLUMN_RE.finditer(migration.sql):
            if match.group("table").lower() == table.lower():
                name = match.group("column")
                if name not in columns:
                    columns.append(name)
    return columns


def _created_columns(sql: str, table: str) -> list[str]:
    """Read the column names out of one ``CREATE TABLE`` body.

    Args:
        sql: The whole migration.
        table: The table of interest.

    Returns:
        The declared column names, constraints skipped; empty when this
        migration does not create that table.
    """
    for match in _CREATE_TABLE_RE.finditer(sql):
        if match.group("table").lower() != table.lower():
            continue
        body = sql[match.end() : sql.index("\n);", match.end())]
        return [
            line.split()[0]
            for raw in body.splitlines()
            if (line := raw.strip()) and not line.upper().startswith(_CONSTRAINT_PREFIXES)
        ]
    return []
