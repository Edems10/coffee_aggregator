from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from coffee_aggregator.db.connect import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    connect,
)

if TYPE_CHECKING:
    import psycopg

    from coffee_aggregator.pipeline import RunReport

logger = logging.getLogger(__name__)

TABLE = "crawl_run"

#: Every column the pipeline fills, in the order :meth:`PostgresMonitor.record`
#: binds them. The migration is the other half of this pair; the integration
#: test asserts the two agree.
COLUMNS: Final[tuple[str, ...]] = (
    "site",
    "started_at",
    "finished_at",
    "duration_s",
    "discovered",
    "fetched",
    "parsed",
    "skipped_non_coffee",
    "failed",
    "disallowed",
    "written",
    "delisted",
    "complete",
    "discovery_ok",
    "deadline_reached",
    "errors",
    "command",
)

_INSERT_SQL = (
    f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) "  # noqa: S608  (a module constant, not input)
    f"VALUES ({', '.join(['%s'] * len(COLUMNS))})"
)
_SELECT_SQL = (
    f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "  # noqa: S608  (same)
    "WHERE (%s::text IS NULL OR site = %s) "
    "ORDER BY started_at DESC, site LIMIT %s"
)

#: What ``runs`` prints when the caller names no limit.
DEFAULT_RECENT_LIMIT = 20


@runtime_checkable
class RunMonitor(Protocol):
    """Somewhere the outcome of one shop's crawl is recorded."""

    def record(self, report: RunReport) -> None:
        """Store one finished run.

        Args:
            report: What the crawl of one shop did.
        """
        ...

    def recent(self, *, site: str | None = None, limit: int = DEFAULT_RECENT_LIMIT) -> list[Run]:
        """Return the runs that finished most recently.

        Args:
            site: Only this shop's runs, or every shop's when None.
            limit: How many rows at most.

        Returns:
            The rows, newest first.
        """
        ...

    def close(self) -> None:
        """Release whatever the monitor holds open."""
        ...


#: One recorded run, as ``runs`` prints it. A plain mapping rather than a
#: dataclass: the row is written from :class:`~coffee_aggregator.pipeline.RunReport`
#: and read back only to be displayed, so a second shape of the same fields
#: would be one more thing to keep in step with the migration.
type Run = dict[str, Any]


class NullMonitor:
    """Records nothing, for the runs that have no database to record into.

    The jsonl sink is the whole reason this exists: a crawl into a flat file is
    a debugging crawl, and making it require a DSN to run at all would be a poor
    trade for observability nobody asked for there.
    """

    def record(self, report: RunReport) -> None:
        """Drop the report on the floor, loudly enough to debug with.

        Args:
            report: What the crawl of one shop did.
        """
        logger.debug("not recording the run of %s: no database configured", report.site_id)

    def recent(self, *, site: str | None = None, limit: int = DEFAULT_RECENT_LIMIT) -> list[Run]:
        """Return nothing, because nothing was ever recorded.

        Args:
            site: Ignored.
            limit: Ignored.

        Returns:
            An empty list.
        """
        logger.debug("no run history without a database (site=%s, limit=%d)", site, limit)
        return []

    def close(self) -> None:
        """Do nothing; there is nothing open."""


class PostgresMonitor:
    """Appends one ``crawl_run`` row per shop per run.

    It owns its own connection rather than borrowing the sink's. The sink's is
    held across a batch upsert and committed with it; a monitor sharing it would
    either be blocked behind that transaction or, worse, be rolled back with it
    — and the row that says "this shop wrote nothing" is at its most valuable
    exactly when the writes are what failed.
    """

    def __init__(
        self,
        dsn: str,
        *,
        command: str = "",
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_S,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    ) -> None:
        """Remember the DSN; the connection is opened on first use.

        Args:
            dsn: A ``postgresql://`` connection string.
            command: The command line this run was started with, stored on every
                row so a surprising night can be reproduced exactly.
            connect_timeout: Seconds to wait for the connection itself.
            statement_timeout_ms: Milliseconds any one statement may run.
        """
        self.dsn = dsn
        self.command = command
        self.connect_timeout = connect_timeout
        self.statement_timeout_ms = statement_timeout_ms
        self._connection: psycopg.Connection[Any] | None = None
        # Shops crawled in parallel finish in parallel, and one psycopg
        # connection carries one transaction.
        self._lock = threading.Lock()

    @property
    def connection(self) -> psycopg.Connection[Any]:
        """Return the live connection, opening it on first access.

        Returns:
            An open psycopg connection with autocommit disabled.
        """
        if self._connection is None or self._connection.closed:
            self._connection = connect(
                self.dsn,
                connect_timeout=self.connect_timeout,
                statement_timeout_ms=self.statement_timeout_ms,
            )
        return self._connection

    def record(self, report: RunReport) -> None:
        """Append one row for a finished run.

        A failure here is logged and swallowed: monitoring that can take the
        crawl down with it is worse than no monitoring at all.

        Args:
            report: What the crawl of one shop did.
        """
        with self._lock:
            try:
                self._record_locked(report)
            except Exception:
                logger.exception("could not record the run of %s", report.site_id)
                self._rollback()

    def _record_locked(self, report: RunReport) -> None:
        """Insert one row with the connection held.

        Args:
            report: What the crawl of one shop did.
        """
        from psycopg.types.json import Jsonb  # noqa: PLC0415  (only the postgres path pays)

        connection = self.connection
        with connection.cursor() as cursor:
            cursor.execute(
                _INSERT_SQL,
                (
                    report.site_id,
                    report.started_at,
                    report.finished_at,
                    report.duration_s,
                    report.discovered,
                    report.fetched,
                    report.parsed,
                    report.skipped_non_coffee,
                    report.failed,
                    report.disallowed,
                    report.written,
                    report.delisted,
                    report.complete,
                    report.discovery_ok,
                    report.deadline_reached,
                    Jsonb(report.errors),
                    self.command,
                ),
            )
        connection.commit()

    def recent(self, *, site: str | None = None, limit: int = DEFAULT_RECENT_LIMIT) -> list[Run]:
        """Return the runs that started most recently.

        Args:
            site: Only this shop's runs, or every shop's when None.
            limit: How many rows at most.

        Returns:
            The rows as mappings keyed by :data:`COLUMNS`, newest first.
        """
        with self._lock, self.connection.cursor() as cursor:
            cursor.execute(_SELECT_SQL, (site, site, max(1, limit)))
            return [dict(zip(COLUMNS, row, strict=True)) for row in cursor.fetchall()]

    def _rollback(self) -> None:
        """Abandon a failed transaction so the next row can still be written."""
        if self._connection is None or self._connection.closed:
            return
        try:
            self._connection.rollback()
        except Exception:  # noqa: BLE001  (a dead connection is reopened on next use)
            logger.warning("could not roll back the monitoring connection")

    def close(self) -> None:
        """Commit anything outstanding and close the connection."""
        with self._lock:
            if self._connection is None or self._connection.closed:
                return
            try:
                self._connection.commit()
            finally:
                self._connection.close()
                self._connection = None


def build_monitor(dsn: str | None, *, command: str = "") -> RunMonitor:
    """Return the monitor that matches where the run is writing.

    Args:
        dsn: The database the run is using, when it uses one.
        command: The command line to stamp on every row.

    Returns:
        A :class:`PostgresMonitor` when there is a database, a
        :class:`NullMonitor` otherwise.
    """
    if not dsn:
        return NullMonitor()
    return PostgresMonitor(dsn, command=command)
