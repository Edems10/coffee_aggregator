from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

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
    "checksum text, "
    "applied_at timestamptz NOT NULL DEFAULT now())"
)
#: Databases migrated before checksums existed have the column added here; the
#: rows keep a NULL checksum until the next ``apply_migrations`` backfills them.
ADD_CHECKSUM_COLUMN_SQL = f"ALTER TABLE {VERSION_TABLE} ADD COLUMN IF NOT EXISTS checksum text"
_SELECT_VERSIONS_SQL = f"SELECT version, checksum FROM {VERSION_TABLE}"  # noqa: S608  (same)
_INSERT_VERSION_SQL = f"INSERT INTO {VERSION_TABLE} (version, checksum) VALUES (%s, %s)"  # noqa: S608  (same)
_BACKFILL_CHECKSUM_SQL = (
    f"UPDATE {VERSION_TABLE} SET checksum = %s "  # noqa: S608  (same)
    "WHERE version = %s AND checksum IS NULL"
)

#: The key of the session-level advisory lock the runner holds while it
#: migrates. Two Lambdas starting at once both find the same file pending and
#: would both run it; one of them then fails on a half-created object, or worse,
#: succeeds at a second ``DELETE``. The lock is session level on purpose: the
#: runner commits between files, and a transaction-level lock would be released
#: by the first of those commits, which is the exact moment it is still needed.
LOCK_KEY: Final = 0x0C0FFEE5
_LOCK_SQL = "SELECT pg_advisory_lock(%s)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(%s)"


class MigrationChecksumError(RuntimeError):
    """An already-applied ``.sql`` file has been edited since it was applied."""


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

    def rollback(self) -> None:
        """Undo the open transaction, so an aborted one stops refusing statements."""


@dataclass(slots=True, frozen=True)
class Migration:
    """One versioned ``.sql`` file."""

    version: str
    sql: str

    @property
    def checksum(self) -> str:
        """The fingerprint recorded when this file is applied.

        Returns:
            The SHA-256 of the file's bytes, hex encoded. It is bookkeeping, not
            a security claim: it only has to change when the file does.
        """
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


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


def _applied(connection: Connection) -> dict[str, str | None]:
    """Return the versions the database already records, with their checksums.

    Args:
        connection: An open connection; the version table is created if missing.

    Returns:
        Every recorded version mapped to the checksum stored for it, which is
        None for a row written before checksums existed.

    Raises:
        MigrationChecksumError: When a recorded file no longer matches what was
            applied.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(CREATE_VERSION_TABLE_SQL)
        cursor.execute(ADD_CHECKSUM_COLUMN_SQL)
        cursor.execute(_SELECT_VERSIONS_SQL)
        rows = cursor.fetchall()
    finally:
        cursor.close()
    connection.commit()
    recorded = {str(row[0]): (None if row[1] is None else str(row[1])) for row in rows}
    _verify_checksums(recorded)
    return recorded


def _verify_checksums(recorded: dict[str, str | None]) -> None:
    """Refuse to work against a database whose migrations were edited.

    An applied file that changed will never be applied again, so the code goes
    on expecting a schema the database does not have — silently, until an upsert
    fails a thousand rows later. Failing here says which file it was.

    Args:
        recorded: Versions mapped to the checksum stored for each.

    Raises:
        MigrationChecksumError: On the first file that does not match.
    """
    for migration in load_migrations():
        stored = recorded.get(migration.version)
        if stored is None or stored == migration.checksum:
            continue
        message = (
            f"migration {migration.version} was applied as {stored[:12]} but the packaged file "
            f"is {migration.checksum[:12]}: an applied migration must never be edited — "
            "add a new one instead"
        )
        raise MigrationChecksumError(message)


def pending(connection: Connection) -> list[str]:
    """Return the versions that still have to be applied, in order.

    This is what ``init-db --dry-run`` prints and what the PostgreSQL sink asks
    before its first write, so it stays cheap: one small SELECT.

    Args:
        connection: An open connection.

    Returns:
        The versions not yet recorded in :data:`VERSION_TABLE`.

    Raises:
        MigrationChecksumError: When an already-applied file has been edited.
    """
    done = _applied(connection)
    return [migration.version for migration in load_migrations() if migration.version not in done]


def apply_migrations(connection: Connection, *, dry_run: bool = False) -> list[str]:
    """Apply every pending migration, each in its own transaction.

    A migration and the row that records it are committed together, so a crash
    can never leave a version half applied or applied twice. Re-running is a
    no-op and returns an empty list. The whole run is serialised behind
    :data:`LOCK_KEY`, so a second process starting at the same moment waits and
    then finds nothing left to do.

    Args:
        connection: An open connection with autocommit disabled.
        dry_run: Only report what would be applied, touching nothing.

    Returns:
        The versions applied by this call, in order — or, for a dry run, the
        versions that would be applied.

    Raises:
        MigrationChecksumError: When an already-applied file has been edited.
    """
    if dry_run:
        return pending(connection)
    with _migration_lock(connection):
        return _apply_locked(connection)


@contextmanager
def _migration_lock(connection: Connection) -> Iterator[None]:
    """Hold the session-level advisory lock for the length of a migration run.

    Args:
        connection: The connection to lock on; the lock lives on the session, so
            the commit after each file does not drop it.

    Yields:
        Nothing; the lock is released when the block ends, however it ends.
    """
    _execute(connection, _LOCK_SQL, (LOCK_KEY,))
    try:
        yield
    finally:
        try:
            # A migration that raised leaves its transaction aborted, and every
            # statement in an aborted transaction is refused — including the one
            # that releases the lock. The lock is session-level, so it would then
            # outlive the failure and block the next run forever. Rolling back
            # first costs nothing when the run succeeded: the work is already
            # committed file by file.
            connection.rollback()
            _execute(connection, _UNLOCK_SQL, (LOCK_KEY,))
        except Exception:  # the session is going away anyway; the lock goes with it
            logger.warning("could not release the migration lock", exc_info=True)


def _execute(connection: Connection, sql: str, params: Sequence[Any] | None = None) -> None:
    """Run one statement and close its cursor.

    Args:
        connection: The connection to run on.
        sql: The statement.
        params: Its bound parameters, when it takes any.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
    finally:
        cursor.close()


def _apply_locked(connection: Connection) -> list[str]:
    """Apply every pending migration with the advisory lock held.

    Args:
        connection: An open connection with autocommit disabled.

    Returns:
        The versions applied by this call, in order.
    """
    done = _applied(connection)
    applied: list[str] = []
    for migration in load_migrations():
        if migration.version in done:
            if done[migration.version] is None:
                _backfill_checksum(connection, migration)
            continue
        logger.info("applying migration %s", migration.version)
        cursor = connection.cursor()
        try:
            cursor.execute(migration.sql)
            cursor.execute(_INSERT_VERSION_SQL, (migration.version, migration.checksum))
        finally:
            cursor.close()
        connection.commit()
        applied.append(migration.version)
    if not applied:
        logger.debug("no pending migrations")
    return applied


def _backfill_checksum(connection: Connection, migration: Migration) -> None:
    """Record the checksum of a file that was applied before checksums existed.

    Args:
        connection: An open connection.
        migration: The already-applied migration to fingerprint.
    """
    _execute(connection, _BACKFILL_CHECKSUM_SQL, (migration.checksum, migration.version))
    connection.commit()


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
