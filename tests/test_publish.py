from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Self

import pytest

from coffee_aggregator import publish
from coffee_aggregator.sinks.outbox import OUTBOX_SQL
from conftest import delisted_row

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType


class FakeCursor:
    """Answers the publisher's three statements out of canned rows."""

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._rows: Sequence[tuple[Any, ...]] = ()
        self.rowcount = 0

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
        self.connection.calls.append((sql, params))
        if sql == publish.CLAIM_SQL:
            self._rows = self.connection.claims.pop(0) if self.connection.claims else ()
        elif sql == publish.RESEED_SQL:
            self._rows = self.connection.catalogue
        self.rowcount = self.connection.deleted if sql == publish.PRUNE_SQL else len(self._rows)

    def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        self.connection.calls.append((sql, list(params_seq)))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        return None


class FakeConnection:
    """Just enough of psycopg.Connection for the publisher."""

    def __init__(
        self,
        claims: list[tuple[tuple[Any, ...], ...]] | None = None,
        catalogue: Sequence[tuple[Any, ...]] = (),
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.claims = claims if claims is not None else []
        self.catalogue = catalogue
        self.deleted = 0
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeBroker:
    """Records what was published, and can refuse to."""

    def __init__(self, *, fail_on: int | None = None) -> None:
        self.sent: list[tuple[str, bytes, str]] = []
        self.fail_on = fail_on
        self.closed = False

    async def publish(self, subject: str, payload: bytes, *, message_id: str) -> None:
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            message = "the broker is not answering"
            raise ConnectionError(message)
        self.sent.append((subject, payload, message_id))

    async def close(self) -> None:
        self.closed = True


def _row(row_id: int, external_id: str = "1") -> tuple[int, str, str]:
    return (row_id, f"coffee.v1.catalogue.demo.{external_id}", json.dumps({"site": "demo"}))


def test_a_batch_is_published_then_marked() -> None:
    connection = FakeConnection(claims=[(_row(1), _row(2, "2"))])
    broker = FakeBroker()

    sent = asyncio.run(publish.publish_batch(connection, broker, limit=500))  # type: ignore[arg-type]

    assert sent == 2
    assert [subject for subject, _, _ in broker.sent] == [
        "coffee.v1.catalogue.demo.1",
        "coffee.v1.catalogue.demo.2",
    ]
    mark = next(call for call in connection.calls if call[0] == publish.MARK_SQL)
    assert mark[1] == ([1, 2],)
    assert connection.commits == 1


def test_the_outbox_id_is_the_message_id() -> None:
    """Delivery is at-least-once; this is what makes the common case effectively-once."""
    connection = FakeConnection(claims=[(_row(17),)])
    broker = FakeBroker()

    asyncio.run(publish.publish_batch(connection, broker))  # type: ignore[arg-type]

    assert broker.sent[0][2] == "outbox-17"


def test_the_claim_locks_its_rows_and_steps_over_another_publisher_s() -> None:
    assert "FOR UPDATE SKIP LOCKED" in publish.CLAIM_SQL
    assert "published_at IS NULL" in publish.CLAIM_SQL
    assert "ORDER BY id" in publish.CLAIM_SQL


def test_an_empty_outbox_marks_nothing_and_leaves_no_transaction_open() -> None:
    connection = FakeConnection()
    broker = FakeBroker()

    assert asyncio.run(publish.publish_batch(connection, broker)) == 0  # type: ignore[arg-type]
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert all(call[0] != publish.MARK_SQL for call in connection.calls)


def test_a_broker_that_refuses_leaves_the_rows_unpublished() -> None:
    """At-least-once is the point: nothing is marked until the ack is in hand."""
    connection = FakeConnection(claims=[(_row(1), _row(2, "2"))])
    broker = FakeBroker(fail_on=1)

    with pytest.raises(ConnectionError):
        asyncio.run(publish.publish_batch(connection, broker))  # type: ignore[arg-type]

    assert connection.commits == 0
    assert all(call[0] != publish.MARK_SQL for call in connection.calls)


def test_draining_keeps_claiming_until_the_table_is_empty() -> None:
    connection = FakeConnection(claims=[(_row(1),), (_row(2, "2"),)])
    broker = FakeBroker()

    assert asyncio.run(publish.drain(connection, broker, limit=1)) == 2  # type: ignore[arg-type]
    assert len(broker.sent) == 2


def test_the_loop_stops_between_batches_when_asked() -> None:
    async def scenario() -> int:
        connection = FakeConnection(claims=[(_row(1),)])
        broker = FakeBroker()
        stop = asyncio.Event()
        task = asyncio.create_task(
            publish.run(connection, broker, idle_sleep_s=0.01, stop=stop)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.05)
        stop.set()
        return await task

    assert asyncio.run(scenario()) == 1


def test_pruning_only_touches_rows_that_were_published() -> None:
    connection = FakeConnection()
    connection.deleted = 3

    assert publish.prune(connection, days=7) == 3  # type: ignore[arg-type]
    assert "published_at IS NOT NULL" in publish.PRUNE_SQL
    assert connection.calls[0] == (publish.PRUNE_SQL, (7,))
    assert connection.commits == 1


def test_republishing_queues_one_event_per_coffee_still_on_sale() -> None:
    connection = FakeConnection(
        catalogue=(
            delisted_row(external_id="1", delisted_at=None),
            delisted_row(external_id="2", delisted_at=None),
        )
    )

    assert publish.reseed(connection) == 2  # type: ignore[arg-type]

    queued = next(call for call in connection.calls if call[0] == OUTBOX_SQL)
    assert [row[0] for row in queued[1]] == [
        "coffee.v1.catalogue.demo.1",
        "coffee.v1.catalogue.demo.2",
    ]
    assert json.loads(queued[1][0][2])["delisted_at"] is None
    assert "delisted_at IS NULL" in publish.RESEED_SQL
    assert connection.commits == 1


def test_republishing_an_empty_catalogue_queues_nothing() -> None:
    connection = FakeConnection()
    assert publish.reseed(connection) == 0  # type: ignore[arg-type]
    assert all(call[0] != OUTBOX_SQL for call in connection.calls)
