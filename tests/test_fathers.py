from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest

from coffee_aggregator.http import FetchResult
from coffee_aggregator.models import ProcessMethod, RoastProfile
from coffee_aggregator.sites import get as get_site
from coffee_aggregator.sites.base import ProductRef
from coffee_aggregator.sites.fathers import FathersSite, parse_props

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee

BASE = "https://fathers.cz"
LIST_URL = f"{BASE}/kava"
CHAPATA_ID = "0cbecbe1-31a0-43b6-87a5-d0a7d38e8598"
DECAF_ID = "f62174a8-1c0a-48ea-b21d-99b6a540ec06"


class FakeFetcher:
    """Serves the canned catalogue page without any network."""

    def __init__(self, bodies: dict[str, str]) -> None:
        self.bodies = bodies
        self.requested: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.requested.append(url)
        body = self.bodies.get(url, "<html><body></body></html>")
        return FetchResult(url, url, 200, body, from_cache=False, elapsed_s=0.0)

    def fetch_many(self, urls: Sequence[str]) -> list[FetchResult]:
        return [self.get(url) for url in urls]


@pytest.fixture
def site() -> FathersSite:
    return FathersSite()


@pytest.fixture
def refs(site: FathersSite, fixture_html: Callable[[str, str], str]) -> list[ProductRef]:
    return site.parse_listing(fixture_html("fathers", "list_kava.html"))


def parse(site: FathersSite, fixture_html: Callable[[str, str], str], name: str) -> Coffee:
    ref = ProductRef(site_id="fathers", external_id="", url=f"{BASE}/kava/espresso/x")
    coffee = site.parse_product(fixture_html("fathers", name), ref)
    assert coffee is not None
    return coffee


@pytest.fixture
def chapata(site: FathersSite, fixture_html: Callable[[str, str], str]) -> Coffee:
    return parse(site, fixture_html, "detail_chapata.html")


@pytest.fixture
def decaf(site: FathersSite, fixture_html: Callable[[str, str], str]) -> Coffee:
    return parse(site, fixture_html, "detail_decaf.html")


# --------------------------------------------------------------------------- registry


def test_the_adapter_registers_itself() -> None:
    adapter = get_site("fathers")
    assert isinstance(adapter, FathersSite)
    assert adapter.base_url == "https://fathers.cz/"
    assert adapter.country == "CZ"
    assert adapter.kind == "roaster"


# --------------------------------------------------------------------------- discovery


def test_the_catalogue_yields_every_coffee_with_real_urls(refs: list[ProductRef]) -> None:
    assert len(refs) == 6  # the fixture keeps six of the shop's 44 coffee records
    ids = [ref.external_id for ref in refs]
    assert len(set(ids)) == len(ids)
    assert CHAPATA_ID in ids
    by_id = {ref.external_id: ref for ref in refs}
    assert by_id[CHAPATA_ID].url == f"{BASE}/kava/espresso/kolumbie-chapata-esp"
    assert all(ref.url.startswith(f"{BASE}/kava/") for ref in refs)
    assert {ref.extra["subcategory"] for ref in refs} <= {"espresso", "filtr", "kapsle", "dripbagy"}


def test_merchandise_never_reaches_discovery(refs: list[ProductRef]) -> None:
    names = {ref.name for ref in refs}
    assert not any("Tričko" in (name or "") for name in names)


def test_every_reference_carries_its_own_payload(refs: list[ProductRef]) -> None:
    for ref in refs:
        assert ref.payload is not None
        payload = json.loads(ref.payload)
        assert payload["product"]["id"] == ref.external_id
        assert payload["currency"] == "CZK"
    chapata = next(ref for ref in refs if ref.external_id == CHAPATA_ID)
    assert chapata.price == 399.8
    assert chapata.currency == "CZK"
    assert chapata.image_url is not None
    assert chapata.image_url.startswith("https://upload.fathers.cz/products/")


def test_discover_costs_exactly_one_request(fixture_html: Callable[[str, str], str]) -> None:
    fake = FakeFetcher({LIST_URL: fixture_html("fathers", "list_kava.html")})
    site = FathersSite()
    refs = list(site.discover(cast("PoliteFetcher", fake)))
    assert fake.requested == [LIST_URL]
    assert len(refs) == 6


def test_a_zero_page_budget_makes_no_request(fixture_html: Callable[[str, str], str]) -> None:
    fake = FakeFetcher({LIST_URL: fixture_html("fathers", "list_kava.html")})
    site = FathersSite()
    assert list(site.discover(cast("PoliteFetcher", fake), max_pages=0)) == []
    assert fake.requested == []


# --------------------------------------------------------------------------- detail pages


def test_chapata_single_origin(chapata: Coffee) -> None:
    assert chapata.name == "Kolumbie - Chapata"
    assert chapata.external_id == CHAPATA_ID
    assert chapata.url == f"{BASE}/kava/espresso/kolumbie-chapata-esp"
    assert (chapata.price, chapata.currency, chapata.weight_g) == (399.8, "CZK", 250)
    assert chapata.price_per_kg == 1599.2
    assert chapata.available is True
    assert chapata.decaf is False
    assert chapata.origin.country == "CO"
    assert chapata.origin.region == "Anserma, Caldas"
    assert chapata.origin.farm == "družstvo Anserma (projekt Attia - Chapata)"
    assert chapata.origin.producer == "Luis Miguel (generální manažer družstva)"
    assert (chapata.origin.altitude_min_m, chapata.origin.altitude_max_m) == (1800, 1800)
    assert chapata.origin.altitude_raw == "1 800 m n. m."
    assert chapata.origin.variety == ["Castillo"]
    assert chapata.processing.method is ProcessMethod.NATURAL
    assert chapata.processing.raw == "suché (natural) s prodlouženou fermentací"
    assert chapata.species.arabica_pct == 100
    assert chapata.species.is_blend is False
    assert chapata.roast.profile is RoastProfile.ESPRESSO


def test_chapata_taste_media_and_flags(chapata: Coffee) -> None:
    assert chapata.taste.flavor_notes == ["kandovaný pomeranč", "badyán", "kakao"]
    assert chapata.taste.tasting_text is not None
    assert "Castillo" in chapata.taste.tasting_text
    assert chapata.taste.brewing_methods == ["espresso"]
    assert len(chapata.images) == 4
    assert all(image.startswith("https://upload.fathers.cz/products/") for image in chapata.images)
    assert chapata.tags == ["Father's Choice", "Novinka"]
    assert chapata.specialty_grade is True
    assert chapata.categories == ["Káva", "espresso"]
    assert chapata.description is not None
    assert "Chapata" in chapata.description
    assert chapata.origin_text == "Kolumbie"


def test_chapata_variants_price_every_package(chapata: Coffee) -> None:
    by_weight = {variant.weight_g: variant for variant in chapata.variants}
    assert set(by_weight) == {250, 1000, 2000}
    assert by_weight[250].price == 399.8
    assert by_weight[1000].price == 1390.59
    assert by_weight[2000].label == "2 kg"
    assert by_weight[250].external_id == "9577413b"
    assert all(variant.currency == "CZK" for variant in chapata.variants)
    assert all(variant.available is True for variant in chapata.variants)


def test_chapata_keeps_every_label_in_raw_attributes(chapata: Coffee) -> None:
    raw = chapata.raw_attributes
    assert len(raw) >= 5
    assert raw["ZEMĚ"] == "Kolumbie"
    assert raw["ODRŮDA"] == "Castillo"
    assert raw["ZPRACOVÁNÍ"] == "suché (natural) s prodlouženou fermentací"
    assert raw["NADMOŘSKÁ VÝŠKA"] == "1 800 m n. m."
    assert raw["TYP KÁVY"] == "pražená zrnková káva, arabika"
    assert "PŘÍBĚH FARMY" in raw  # the shop's long-form sections are kept too
    assert "METAKEYWORDS" in raw
    assert all(key == key.upper() == key.strip() for key in raw)


def test_the_decaf_lot_is_flagged_and_fully_parsed(decaf: Coffee) -> None:
    assert decaf.name == "DECAF Atunkaa – Kolumbie"
    assert decaf.external_id == DECAF_ID
    assert decaf.decaf is True
    assert decaf.origin.country == "CO"
    assert decaf.origin.region == "Rio Sucio, Caldas"
    assert decaf.origin.farm == "Siruma Coffee farmy"
    assert (decaf.origin.altitude_min_m, decaf.origin.altitude_max_m) == (1600, 2000)
    assert decaf.origin.variety == ["Castillo", "Colombia"]
    assert decaf.processing.method is ProcessMethod.WASHED
    assert decaf.taste.flavor_notes == ["pomerančový koláč", "třtinový cukr", "bílý čaj"]
    assert (decaf.price, decaf.currency, decaf.weight_g) == (399.8, "CZK", 250)
    assert len(decaf.raw_attributes) >= 5


# --------------------------------------------------------------------------- payload path


def test_a_reference_payload_parses_to_the_same_coffee(
    site: FathersSite,
    refs: list[ProductRef],
    chapata: Coffee,
) -> None:
    """The pipeline parses ``payload`` directly, so it must match the page."""
    ref = next(item for item in refs if item.external_id == CHAPATA_ID)
    assert ref.payload is not None
    from_payload = site.parse_product(ref.payload, ref)
    assert from_payload is not None
    assert from_payload.to_record()["raw_attributes"] == chapata.to_record()["raw_attributes"]
    assert from_payload.price == chapata.price
    assert from_payload.origin.country == chapata.origin.country
    assert from_payload.url == chapata.url


# --------------------------------------------------------------------------- edge cases


@pytest.mark.parametrize(
    "name",
    ["Sample box", "Kapsle - Etiopie Lalesa Natural", "Drip bag – mix", "Inmaculada Tasting Box"],
)
def test_non_bean_products_are_skipped(site: FathersSite, name: str) -> None:
    assert site.is_ignored(name) is True
    payload = json.dumps({"product": {"id": "x", "name": {"cs": name}}})
    assert site.parse_product(payload, ProductRef("fathers", "x", f"{BASE}/kava/filtr/x")) is None


def test_the_catalogue_lists_boxes_that_parsing_then_drops(
    site: FathersSite,
    refs: list[ProductRef],
) -> None:
    kept = [ref for ref in refs if not site.is_ignored(ref.name)]
    assert len(kept) == 4  # six records minus one sample box and one capsule pack
    assert {"Sample box", "Kapsle - Etiopie Lalesa Natural"} & {ref.name for ref in kept} == set()


def test_a_source_without_a_product_record_yields_none(site: FathersSite) -> None:
    ref = ProductRef("fathers", "x", f"{BASE}/kava/espresso/x")
    assert site.parse_product("<html><body>nothing here</body></html>", ref) is None
    assert site.parse_product("{not json", ref) is None


def test_parse_props_reads_both_a_page_and_a_payload() -> None:
    assert parse_props('{"currency": "CZK"}') == {"currency": "CZK"}
    page = '<script id="props" type="application/json">{"currency":"EUR"}</script>'
    assert parse_props(page) == {"currency": "EUR"}
    assert parse_props("<html></html>") == {}


def test_a_product_that_states_no_price_keeps_its_other_fields(site: FathersSite) -> None:
    payload = json.dumps(
        {
            "product": {
                "id": "abc",
                "name": {"cs": "Testovací káva"},
                "urlSlug_cs": "testovaci-kava",
                "plainDescription": {"cs": "Země: Keňa\nOdrůda: SL28\nZpracování: promyté"},
                "variants": [],
            },
            "subcategory": {"urlSlug_cs": "filtr"},
        }
    )
    coffee = site.parse_product(payload, ProductRef("fathers", "abc", f"{BASE}/kava/filtr/x"))
    assert coffee is not None
    assert coffee.price is None
    assert coffee.weight_g is None
    assert coffee.available is None
    assert coffee.origin.country == "KE"
    assert coffee.origin.variety == ["SL28"]
    assert coffee.processing.method is ProcessMethod.WASHED
    assert coffee.roast.profile is RoastProfile.FILTER
    assert coffee.url == f"{BASE}/kava/filtr/testovaci-kava"


def test_a_url_inside_the_description_is_not_read_as_a_label(site: FathersSite) -> None:
    payload = json.dumps(
        {
            "product": {
                "id": "abc",
                "name": {"cs": "Testovací káva"},
                "plainDescription": {"cs": "Země: Peru\nvíc zde\n[https://fathers.cz/blog/x]"},
            }
        }
    )
    coffee = site.parse_product(payload, ProductRef("fathers", "abc", f"{BASE}/kava/filtr/x"))
    assert coffee is not None
    assert list(coffee.raw_attributes) == ["ZEMĚ"]
    assert coffee.description is not None
    assert "https://fathers.cz/blog/x" in coffee.description
