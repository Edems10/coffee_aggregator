from __future__ import annotations

import json
from typing import TYPE_CHECKING

from coffee_aggregator.models import Coffee
from coffee_aggregator.sinks.jsonl import JsonlSink
from coffee_aggregator.sinks.postgres import COLUMNS

if TYPE_CHECKING:
    from pathlib import Path
from conftest import make_coffee


def test_jsonl_sink_writes_one_valid_json_line_per_coffee(tmp_path: Path) -> None:
    path = tmp_path / "out" / "coffees.jsonl"
    sink = JsonlSink(path)
    result = sink.upsert([make_coffee(external_id="1"), make_coffee(external_id="2")])
    sink.close()

    assert result.written == 2
    assert result.failed == 0
    lines = path.read_text("utf-8").strip().splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert [record["external_id"] for record in records] == ["1", "2"]


def test_jsonl_sink_keeps_unicode_unescaped(tmp_path: Path) -> None:
    path = tmp_path / "coffees.jsonl"
    sink = JsonlSink(path)
    sink.upsert([make_coffee()])
    sink.close()
    assert "Káva" in path.read_text("utf-8")


def test_jsonl_sink_cannot_delist(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path / "c.jsonl")
    assert sink.mark_delisted("demo", {"1"}) == 0
    sink.close()


def test_to_record_keys_are_exactly_the_sink_columns() -> None:
    assert list(make_coffee().to_record().keys()) == list(COLUMNS)


def test_to_record_round_trips_every_field() -> None:
    coffee = make_coffee()
    record = coffee.to_record(json_safe=True)

    assert record["site"] == "demo"
    assert record["price_per_kg"] == 49.95
    assert record["origin_country"] == "CU"
    assert record["altitude_min_m"] == 1200
    assert record["variety"] == ["Typica", "Bourbon"]
    assert record["process_method"] == "washed"
    assert record["roast_level"] == "medium"
    assert record["roast_profile"] == "omni"
    assert record["roast_date"] == "2026-09-01"
    assert record["best_before"] == "2027-09-01"
    assert record["arabica_pct"] == 100
    assert record["is_blend"] is False
    assert record["flavor_notes"] == ["kakao", "karamel"]
    assert record["brewing_methods"] == ["espresso", "moka"]
    assert record["sca_score"] == 84.5
    assert record["reviews"][0]["author"] == "Jana"
    assert record["reviews"][0]["date"] == "2026-01-02"
    assert record["variants"][0]["weight_g"] == 250
    assert record["images"] == ["https://example.sk/img/1.jpg"]
    assert record["certifications"] == ["BIO"]
    assert record["awards"] == ["Cup of Excellence"]
    assert record["raw_attributes"]["KRAJINA"] == "Kuba"
    assert record["scraped_at"] == "2026-09-12T10:00:00+00:00"
    assert json.loads(json.dumps(record, ensure_ascii=False)) == record


def test_to_record_keeps_native_dates_for_postgres() -> None:
    record = make_coffee().to_record(json_safe=False)
    assert record["roast_date"].year == 2026
    assert record["scraped_at"].tzinfo is not None


def test_price_per_kg_is_none_without_price_or_weight() -> None:
    assert Coffee("s", "1", "u", "n", "SK", price=None, weight_g=250).price_per_kg is None
    assert Coffee("s", "1", "u", "n", "SK", price=5.0, weight_g=None).price_per_kg is None
    assert Coffee("s", "1", "u", "n", "SK", price=5.0, weight_g=0).price_per_kg is None


def test_a_crawl_that_writes_nothing_leaves_the_previous_output_alone(tmp_path: Path) -> None:
    path = tmp_path / "coffees.jsonl"
    path.write_text('{"external_id": "yesterday"}\n', encoding="utf-8")

    sink = JsonlSink(path)
    sink.close()

    assert path.read_text("utf-8") == '{"external_id": "yesterday"}\n'


def test_a_crawl_that_dies_halfway_leaves_the_previous_output_alone(tmp_path: Path) -> None:
    path = tmp_path / "coffees.jsonl"
    path.write_text('{"external_id": "yesterday"}\n', encoding="utf-8")

    sink = JsonlSink(path)
    sink.upsert([make_coffee(external_id="1")])
    # …and then the process dies: close() is never reached
    assert path.read_text("utf-8") == '{"external_id": "yesterday"}\n'
    assert sink.temporary.is_file()


def test_the_finished_file_replaces_the_previous_one_on_close(tmp_path: Path) -> None:
    path = tmp_path / "coffees.jsonl"
    path.write_text('{"external_id": "yesterday"}\n', encoding="utf-8")

    sink = JsonlSink(path)
    sink.upsert([make_coffee(external_id="1")])
    sink.close()

    records = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    assert [record["external_id"] for record in records] == ["1"]
    assert not sink.temporary.exists()


def test_closing_twice_is_harmless(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path / "c.jsonl")
    sink.upsert([make_coffee()])
    sink.close()
    sink.close()
    assert (tmp_path / "c.jsonl").is_file()
