from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

    import psycopg

#: Seconds to wait for the TCP connection and the authentication handshake. The
#: default is none at all, so an unreachable database hangs a Lambda until the
#: 15-minute invocation timeout kills it with no log line worth reading.
DEFAULT_CONNECT_TIMEOUT_S: Final = 10
#: Milliseconds any single statement may run for. An upsert of 200 rows takes
#: milliseconds; anything near this is a lock we are never getting.
DEFAULT_STATEMENT_TIMEOUT_MS: Final = 60_000


def connect(
    dsn: str,
    *,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_S,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> psycopg.Connection[Any]:
    """Open the one kind of connection every part of the project uses.

    ``prepare_threshold=None`` is the load-bearing argument. psycopg prepares a
    statement on the server after it has seen it five times, and the crawl sends
    the same upsert thousands of times; a server-prepared statement pins the
    session to one backend, which is precisely what stops RDS Proxy multiplexing
    and turns a pool of ten backends into a pool of one per Lambda.

    Args:
        dsn: A ``postgresql://`` connection string.
        connect_timeout: Seconds to wait for the connection itself.
        statement_timeout_ms: Milliseconds any one statement may run.

    Returns:
        An open connection with autocommit disabled and both timeouts set.
    """
    import psycopg  # noqa: PLC0415  (only the postgres paths pay for the driver)

    return psycopg.connect(
        dsn,
        autocommit=False,
        prepare_threshold=None,
        connect_timeout=connect_timeout,
        options=f"-c statement_timeout={statement_timeout_ms}",
    )


@runtime_checkable
class Cursor(Protocol):
    """The slice of a psycopg cursor the read-only queries use."""

    def execute(self, query: str, params: Sequence[Any] | None = None, /) -> Any:  # noqa: ANN401
        """Run one statement.

        Args:
            query: The SQL, with ``%s`` placeholders.
            params: What to bind to them.

        Returns:
            Whatever the driver returns; callers read rows with ``fetchall``.
        """
        ...

    def fetchall(self) -> list[Any]:
        """Return every remaining row of the last statement.

        Returns:
            One tuple per row, in the order the query asked for.
        """
        ...

    def __enter__(self) -> Self:
        """Return the cursor itself, so it can be used in a ``with``.

        Returns:
            This cursor.
        """
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None:
        """Close the cursor when the ``with`` block ends.

        Args:
            exc_type: The class of the exception leaving the block, if any.
            exc: The exception itself, if any.
            traceback: Its traceback, if any.
        """
        ...


@runtime_checkable
class Connection(Protocol):
    """A database to read from.

    Deliberately narrower than ``psycopg.Connection``: everything that only
    reads — :mod:`coffee_aggregator.db.report` above all — takes this instead,
    so a test can hand it a few lists of tuples rather than a live PostgreSQL.
    A real connection satisfies it structurally; nothing has to be registered.
    """

    def cursor(self) -> Cursor:
        """Open a cursor on this connection.

        Returns:
            A cursor usable as a context manager.
        """
        ...
