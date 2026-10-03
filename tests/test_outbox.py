from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from coffee_contracts import COFFEE_STATE, CoffeeState, coffee_from_json, to_json

from coffee_aggregator.sinks import outbox
from coffee_aggregator.sinks.postgres import COLUMNS, MANAGED_COLUMNS, PostgresSink
from conftest import delisted_row, make_coffee
from test_postgres_sink import FakeConnection


def _payload(row: tuple[str, str, str]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(row[2])
    return loaded


def test_state_carries_what_a_consumer_can_act_on() -> None:
    row = outbox.event_rows_for([make_coffee(site="demo", external_id="42")])[0]

    assert row[0] == "coffee.v1.catalogue.demo.42"
    assert row[1] == COFFEE_STATE
    payload = _payload(row)
    assert payload["type"] == COFFEE_STATE
    assert payload["name"] == "Kuba Serrano Superior"
    assert payload["origin_country"] == "CU"
    assert payload["roast_level"] == "medium"
    assert payload["flavor_notes"] == ["kakao", "karamel"]
    assert payload["observed_at"] == "2026-09-12T10:00:00+00:00"
    assert payload["delisted_at"] is None


def test_the_payload_round_trips_through_the_contract() -> None:
    coffee = make_coffee(site="demo", external_id="42")
    state = coffee_from_json(outbox.event_rows_for([coffee])[0][2])

    assert state.key == "demo/42"
    assert state.observed_at == datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
    assert state.is_delisted is False


def test_the_roaster_comes_from_the_brand_attribute_when_nothing_else_states_it() -> None:
    coffee = make_coffee(raw_attributes={"BRAND": "Doubleshot"})
    assert _payload(outbox.event_rows_for([coffee])[0])["roaster"] == "Doubleshot"


def test_an_empty_external_id_is_skipped_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    """coffeein and nordbeans both fall back to "" when a listing gives no id.

    Raising here would abort the batch, send it down ``_retry_one_by_one`` and
    drop the coffee from the catalogue as well: a data regression caused by a
    messaging concern.
    """
    good = make_coffee(site="coffeein", external_id="7")
    bad = make_coffee(site="coffeein", external_id="", url="https://coffeein.sk/p/mystery")

    with caplog.at_level(logging.WARNING, logger=outbox.logger.name):
        rows = outbox.event_rows_for([good, bad])

    assert [row[0] for row in rows] == ["coffee.v1.catalogue.coffeein.7"]
    assert "https://coffeein.sk/p/mystery" in caplog.text
    assert "external_id is empty" in caplog.text


def test_a_site_id_with_a_dot_is_skipped_too() -> None:
    """A dot is structure in a NATS subject, so it would silently widen a subscription."""
    assert outbox.event_rows_for([make_coffee(site="demo.sk", external_id="7")]) == []


def test_every_state_expression_reads_a_column_the_coffee_table_has() -> None:
    """The RETURNING list and the dataclass drift apart the moment one is edited alone."""
    known = {*COLUMNS, *MANAGED_COLUMNS}
    bare = [name for name in outbox.STATE_EXPRESSIONS if name.isidentifier()]
    assert set(bare) <= known
    assert len(outbox.STATE_EXPRESSIONS) == len(outbox.STATE_FIELDS)


def test_a_row_read_back_out_of_the_table_becomes_the_same_kind_of_event() -> None:
    state = outbox.state_from_row(delisted_row(external_id="9"))

    assert state.key == "demo/9"
    assert state.is_delisted
    # psycopg hands numerics back as Decimal, which the contract's encoder
    # refuses outright rather than guess a representation for.
    assert state.price == 9.99
    assert isinstance(state.price, float)
    assert state.sca_score == 84.5
    assert state.variety == ["Typica"]


def test_a_null_jsonb_column_becomes_an_empty_list() -> None:
    state = outbox.state_from_row(delisted_row(variety=None, flavor_notes=None))
    assert state.variety == []
    assert state.flavor_notes == []


def test_a_row_read_back_serialises() -> None:
    rows = outbox.event_rows_from_db([delisted_row(external_id="9")])
    assert _payload(rows[0])["price"] == 9.99


def test_the_decimal_guard_is_load_bearing() -> None:
    """Without the coercion the contract's encoder refuses the event outright."""
    straight = CoffeeState(
        site="demo",
        external_id="9",
        observed_at=datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
        price=Decimal("9.99"),  # type: ignore[arg-type]
    )
    with pytest.raises(TypeError, match="Decimal is not part of the contract"):
        to_json(straight)


def test_the_upsert_queues_one_event_per_coffee_in_the_same_transaction() -> None:
    connection = FakeConnection()
    sink = PostgresSink("postgresql://fake/db", chunk_size=10)
    sink._connection = connection  # type: ignore[assignment]
    sink._schema_checked = True

    sink.upsert([make_coffee(external_id="1"), make_coffee(external_id="2")])

    events = next(call for call in connection.calls if call[1] == outbox.OUTBOX_SQL)
    assert events[0] == "executemany"
    assert [row[0] for row in events[2]] == [
        "coffee.v1.catalogue.demo.1",
        "coffee.v1.catalogue.demo.2",
    ]
    assert connection.commits == 1


def test_a_coffee_with_no_id_is_still_stored_even_though_it_is_not_published() -> None:
    connection = FakeConnection()
    sink = PostgresSink("postgresql://fake/db", chunk_size=10)
    sink._connection = connection  # type: ignore[assignment]
    sink._schema_checked = True

    result = sink.upsert([make_coffee(external_id="")])

    assert result.written == 1
    assert result.failed == 0
    assert all(call[1] != outbox.OUTBOX_SQL for call in connection.calls)
