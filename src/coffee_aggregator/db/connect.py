from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
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
