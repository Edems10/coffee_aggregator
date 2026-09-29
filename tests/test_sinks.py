from __future__ import annotations

import json
from typing import TYPE_CHECKING

from coffee_aggregator.models import Coffee, Variant
from coffee_aggregator.sinks.jsonl import JsonlSink
from coffee_aggregator.sinks.postgres import COLUMNS, VARIANT_COLUMNS, variant_rows_for

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


def _with_variants(*variants: Variant, price: float | None, weight_g: int | None) -> Coffee:
    return Coffee(
        "s",
        "1",
        "u",
        "n",
        "SK",
        price=price,
        currency="CZK",
        weight_g=weight_g,
        variants=[*variants],
    )


def test_the_per_kilo_price_comes_from_the_variant_that_states_the_price() -> None:
    """The product name said 1 kg; the bag the price is for holds 125 g."""
    coffee = _with_variants(
        Variant(external_id="a", weight_g=125, price=690.0, currency="CZK"),
        Variant(external_id="b", weight_g=1000, price=4200.0, currency="CZK"),
        price=690.0,
        weight_g=1000,
    )

    basis = coffee.price_basis
    assert basis is not None
    assert (basis.price, basis.weight_g, basis.source) == (690.0, 125, "variant")
    assert coffee.price_per_kg == 5520.0


def test_two_bags_sharing_one_price_leave_the_per_kilo_price_empty() -> None:
    """The page simply does not say which bag 260 CZK buys."""
    coffee = _with_variants(
        Variant(external_id="a", weight_g=125, price=260.0),
        Variant(external_id="b", weight_g=250, price=260.0),
        price=260.0,
        weight_g=125,
    )

    assert coffee.price_basis is None
    assert coffee.price_per_kg is None
    assert coffee.to_record()["price_per_kg"] is None


def test_the_product_pair_is_used_when_no_variant_claims_the_price() -> None:
    coffee = _with_variants(
        Variant(external_id="a", weight_g=1000, price=999.0),
        price=287.0,
        weight_g=250,
    )

    basis = coffee.price_basis
    assert basis is not None
    assert (basis.weight_g, basis.source) == (250, "product")
    assert coffee.price_per_kg == 1148.0


def test_a_variant_with_no_weight_never_decides_anything() -> None:
    coffee = _with_variants(
        Variant(external_id="a", weight_g=None, price=95.0),
        price=95.0,
        weight_g=70,
    )

    assert coffee.price_per_kg == 1357.14


def test_the_variant_key_prefers_the_shops_own_id_and_never_the_position() -> None:
    assert Variant(external_id="5327/250", label="250 g").key(3) == "5327/250"
    assert Variant(label="250 g").key(3) == "250 g"
    assert Variant(weight_g=250).key(3) == "250g"
    assert Variant().key(3) == "#3"


def test_the_roaster_is_read_out_of_the_brand_attribute() -> None:
    coffee = make_coffee(raw_attributes={"BRAND": "Doubleshot  "})
    record = coffee.to_record()

    assert record["roaster"] == "Doubleshot"
    assert record["roaster_key"] == "doubleshot"


def test_the_roaster_key_folds_accents_so_two_shops_match() -> None:
    written = make_coffee(raw_attributes={"BRAND": "Pražírna Mlýnek"})
    other = make_coffee(raw_attributes={"BRAND": "PRAZIRNA MLYNEK"})

    assert written.roaster_key == other.roaster_key == "prazirna mlynek"


def test_a_product_with_no_brand_keeps_both_roaster_columns_empty() -> None:
    record = make_coffee().to_record()
    assert record["roaster"] is None
    assert record["roaster_key"] is None


def test_an_explicit_roaster_wins_over_the_raw_attribute() -> None:
    coffee = make_coffee(raw_attributes={"BRAND": "Shop"}, roaster="Roastery")
    assert coffee.to_record()["roaster"] == "Roastery"


def test_variant_rows_carry_everything_a_cross_shop_query_needs() -> None:
    coffee = make_coffee(external_id="7")
    rows = [dict(zip(VARIANT_COLUMNS, row, strict=True)) for row in variant_rows_for(coffee)]

    assert len(rows) == 1
    assert rows[0]["site"] == "demo"
    assert rows[0]["external_id"] == "7"
    assert rows[0]["variant_key"] == "1-250"
    assert rows[0]["weight_g"] == 250
    assert rows[0]["price"] == 11.5
    assert rows[0]["currency"] == "EUR"
    assert rows[0]["available"] is True


def test_a_variant_inherits_the_products_currency_when_it_states_none() -> None:
    coffee = make_coffee(variants=[Variant(external_id="a", weight_g=250, price=11.5)])
    row = dict(zip(VARIANT_COLUMNS, variant_rows_for(coffee)[0], strict=True))
    assert row["currency"] == "EUR"


def test_two_variants_sharing_one_key_are_written_once() -> None:
    """PostgreSQL refuses to touch the same row twice inside one statement."""
    coffee = make_coffee(
        variants=[Variant(label="250 g", price=11.5), Variant(label="250 g", price=12.0)]
    )
    rows = variant_rows_for(coffee)
    assert [row[2] for row in rows] == ["250 g"]
