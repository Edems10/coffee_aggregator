from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from itertools import batched
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from coffee_aggregator.config import DEFAULT_NATS_URL
from coffee_aggregator.db.connect import connect
from coffee_aggregator.sinks import outbox

if TYPE_CHECKING:
    from collections.abc import Sequence

    import psycopg

logger = logging.getLogger(__name__)

#: How many rows one transaction claims. Large enough that a full catalogue
#: drains in a handful of round trips, small enough that the rows stay locked
#: for about a second even if the broker is answering slowly.
DEFAULT_BATCH_SIZE = 500
#: How long to wait before asking an empty table again.
DEFAULT_IDLE_SLEEP_S = 2.0
#: How long a published row is kept. It is evidence, not state — the catalogue
#: itself is in ``coffee`` — so a week is long enough to answer "was this ever
#: sent, and when" while a crash is still being investigated.
RETENTION_DAYS = 7
#: Pruning a week-old tail is not urgent, so it runs on an idle pass at most
#: this often rather than on every drain.
PRUNE_INTERVAL_S = 3600.0

#: What the duplicate detection on the broker matches on. The publisher is
#: at-least-once by construction — it can send a message and then fail before
#: the row is marked — so the row's own id goes out with it, and the stream's
#: duplicate window turns a redelivery into a no-op.
MESSAGE_ID_HEADER = "Nats-Msg-Id"
MESSAGE_ID_PREFIX = "outbox-"

#: ``payload::text`` rather than ``payload``: psycopg decodes a ``jsonb`` column
#: into a Python dict, which would then have to be encoded again to be sent. The
#: text is already the body.
CLAIM_SQL = (
    "SELECT id, subject, payload::text FROM outbox WHERE published_at IS NULL "
    "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED"
)
MARK_SQL = "UPDATE outbox SET published_at = now() WHERE id = ANY(%s)"
PRUNE_SQL = (
    "DELETE FROM outbox WHERE published_at IS NOT NULL "
    "AND published_at < now() - make_interval(days => %s)"
)

#: Only what is still listed. A delisted coffee that is re-seeded would announce
#: itself as gone, which is correct but pointless: the stream keeps one message
#: per coffee, so a consumer replaying a re-seeded stream never learns of it at
#: all, which is the same catalogue by a shorter route.
RESEED_SQL = (
    f"SELECT {outbox.STATE_SELECT} FROM coffee "  # noqa: S608  (a module constant)
    "WHERE delisted_at IS NULL ORDER BY site, external_id"
)
#: How many outbox rows one ``executemany`` of a re-seed carries.
RESEED_CHUNK = 500


@runtime_checkable
class Broker(Protocol):
    """Somewhere an event can be published, with an ack worth waiting for."""

    async def publish(self, subject: str, payload: bytes, *, message_id: str) -> None:
        """Send one message and wait for the broker to say it stored it.

        Args:
            subject: The subject to publish on.
            payload: The message body.
            message_id: The de-duplication id to send it under.
        """
        ...

    async def close(self) -> None:
        """Flush anything outstanding and disconnect."""
        ...


class NatsBroker:
    """A JetStream connection that acknowledges every publish."""

    def __init__(self, connection: Any, stream: Any) -> None:  # noqa: ANN401  (nats types are only importable lazily)
        """Wrap an open connection.

        Args:
            connection: The live ``nats.aio.client.Client``.
            stream: Its JetStream context.
        """
        self._connection = connection
        self._stream = stream

    @classmethod
    async def connect(cls, url: str) -> NatsBroker:
        """Open a connection to the broker.

        Args:
            url: The ``nats://`` URL to connect to.

        Returns:
            The connected broker.
        """
        import nats  # noqa: PLC0415  (only the publisher pays for the client)

        logger.info("connecting to %s", url)
        # The publisher is a long-running service whose whole job is this
        # connection, so it reconnects for as long as the broker is away rather
        # than exiting after the client's default ten attempts and leaving the
        # queue to grow behind a container in a restart loop.
        connection = await nats.connect(url, max_reconnect_attempts=-1)
        return cls(connection, connection.jetstream())

    async def publish(self, subject: str, payload: bytes, *, message_id: str) -> None:
        """Publish one message and await the stream's ack.

        Args:
            subject: The subject to publish on.
            payload: The message body.
            message_id: The de-duplication id to send it under.
        """
        await self._stream.publish(subject, payload, headers={MESSAGE_ID_HEADER: message_id})

    async def close(self) -> None:
        """Drain the connection, which flushes before it closes."""
        await self._connection.drain()


async def publish_batch(
    connection: psycopg.Connection[Any],
    broker: Broker,
    *,
    limit: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Claim, publish and mark one batch of unpublished rows.

    The rows stay locked by ``FOR UPDATE SKIP LOCKED`` while the broker is being
    waited on, which is what lets a second publisher run beside this one and
    take the next batch rather than the same one.

    Args:
        connection: An open connection with autocommit disabled.
        broker: Where to publish.
        limit: How many rows to claim.

    Returns:
        How many messages were published.
    """
    with connection.cursor() as cursor:
        cursor.execute(CLAIM_SQL, (limit,))
        rows: Sequence[tuple[int, str, str]] = cursor.fetchall()
        if not rows:
            # Nothing was changed, but the transaction has a snapshot open and
            # an idle-in-transaction connection holds the vacuum back.
            connection.rollback()
            return 0
        for row_id, subject, payload in rows:
            await broker.publish(
                subject,
                payload.encode(),
                message_id=f"{MESSAGE_ID_PREFIX}{row_id}",
            )
        cursor.execute(MARK_SQL, ([row[0] for row in rows],))
    connection.commit()
    logger.info("published %d event(s), through outbox id %d", len(rows), rows[-1][0])
    return len(rows)


def prune(connection: psycopg.Connection[Any], *, days: int = RETENTION_DAYS) -> int:
    """Delete rows published longer ago than the retention window.

    Args:
        connection: An open connection with autocommit disabled.
        days: How long a published row is kept.

    Returns:
        How many rows were deleted.
    """
    with connection.cursor() as cursor:
        cursor.execute(PRUNE_SQL, (days,))
        deleted = cursor.rowcount
    connection.commit()
    if deleted > 0:
        logger.info("pruned %d outbox row(s) published over %d days ago", deleted, days)
    return max(0, deleted)


def reseed(connection: psycopg.Connection[Any], *, chunk: int = RESEED_CHUNK) -> int:
    """Write an outbox row for every coffee still on sale.

    This is what makes the broker's store and every consumer's store disposable:
    the catalogue in ``coffee`` is the record, and a stream that was lost, moved
    or re-created is refilled from it without a crawl and without asking the
    shops for anything. It is also the honest answer to "what backs up the
    stream" — nothing does, because nothing needs to.

    Args:
        connection: An open connection with autocommit disabled.
        chunk: How many rows one ``executemany`` carries.

    Returns:
        How many events were queued.
    """
    with connection.cursor() as cursor:
        cursor.execute(RESEED_SQL)
        rows = outbox.event_rows_from_db(cursor.fetchall())
        for part in batched(rows, chunk, strict=False):
            cursor.executemany(outbox.OUTBOX_SQL, list(part))
    connection.commit()
    logger.info("queued %d event(s) for re-publication", len(rows))
    return len(rows)


def republish(dsn: str, *, chunk: int = RESEED_CHUNK) -> int:
    """Re-seed the outbox from the catalogue, opening the connection itself.

    Args:
        dsn: A ``postgresql://`` connection string.
        chunk: How many rows one ``executemany`` carries.

    Returns:
        How many events were queued.
    """
    connection = connect(dsn)
    try:
        return reseed(connection, chunk=chunk)
    finally:
        connection.close()


async def drain(
    connection: psycopg.Connection[Any],
    broker: Broker,
    *,
    limit: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Publish everything waiting and return.

    Args:
        connection: An open connection with autocommit disabled.
        broker: Where to publish.
        limit: How many rows one batch claims.

    Returns:
        How many messages were published in total.
    """
    total = 0
    while (sent := await publish_batch(connection, broker, limit=limit)) > 0:
        total += sent
    return total


async def run(
    connection: psycopg.Connection[Any],
    broker: Broker,
    *,
    limit: int = DEFAULT_BATCH_SIZE,
    idle_sleep_s: float = DEFAULT_IDLE_SLEEP_S,
    stop: asyncio.Event | None = None,
) -> int:
    """Drain the outbox until asked to stop.

    Args:
        connection: An open connection with autocommit disabled.
        broker: Where to publish.
        limit: How many rows one batch claims.
        idle_sleep_s: How long to wait before asking an empty table again.
        stop: Set to end the loop; one is created when none is given, which
            makes the loop run until the process is killed.

    Returns:
        How many messages were published in total.
    """
    halt = asyncio.Event() if stop is None else stop
    loop = asyncio.get_running_loop()
    total = 0
    since_prune = loop.time() - PRUNE_INTERVAL_S
    while not halt.is_set():
        sent = await publish_batch(connection, broker, limit=limit)
        total += sent
        if sent > 0:
            continue
        if loop.time() - since_prune >= PRUNE_INTERVAL_S:
            prune(connection)
            since_prune = loop.time()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(halt.wait(), idle_sleep_s)
    return total


async def serve(
    dsn: str,
    *,
    url: str = DEFAULT_NATS_URL,
    limit: int = DEFAULT_BATCH_SIZE,
    once: bool = False,
) -> int:
    """Open the connections the publisher needs and run it.

    Args:
        dsn: A ``postgresql://`` connection string.
        url: The ``nats://`` URL of the broker.
        limit: How many rows one batch claims.
        once: Publish what is waiting and return, instead of looping.

    Returns:
        How many messages were published.
    """
    connection = connect(dsn)
    broker = await NatsBroker.connect(url)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    try:
        if once:
            return await drain(connection, broker, limit=limit)
        return await run(connection, broker, limit=limit, stop=stop)
    finally:
        await broker.close()
        connection.close()


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Make ``docker stop`` end the loop between batches rather than mid-publish.

    A batch is already committed or already rolled back at every point the loop
    can be interrupted at, so the worst a stop costs is a batch republished.

    Args:
        stop: The event to set when a termination signal arrives.
    """
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not every platform has them
            loop.add_signal_handler(number, stop.set)
