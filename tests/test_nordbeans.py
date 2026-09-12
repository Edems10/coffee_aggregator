from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from coffee_aggregator.http import FetchResult
from coffee_aggregator.models import ProcessMethod, RoastProfile
from coffee_aggregator.sites import get as get_site
from coffee_aggregator.sites.base import ProductRef
from coffee_aggregator.sites.nordbeans import NordbeansSite, external_id_of, split_notes

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee

BASE = "https://www.nordbeans.cz"
ESPRESSO_URL = f"{BASE}/espresso/"
FILTER_URL = f"{BASE}/filtrovana-kava/"


class FakeFetcher:
    """Serves canned category pages without any network."""

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
def site() -> NordbeansSite:
    return NordbeansSite()


@pytest.fixture
def refs(site: NordbeansSite, fixture_html: Callable[[str, str], str]) -> list[ProductRef]:
    return site.parse_listing(fixture_html("nordbeans", "list_espresso.html"))


def parse(
    site: NordbeansSite,
    fixture_html: Callable[[str, str], str],
    name: str,
    external_id: str,
) -> Coffee:
    ref = ProductRef(
        site_id="nordbeans",
        external_id=external_id,
        url=f"{BASE}/produkt_z{external_id}/",
    )
    coffee = site.parse_product(fixture_html("nordbeans", name), ref)
    assert coffee is not None
    return coffee


@pytest.fixture
def gakenke(site: NordbeansSite, fixture_html: Callable[[str, str], str]) -> Coffee:
    return parse(site, fixture_html, "detail_3984_gakenke.html", "3984")


@pytest.fixture
def house_blend(site: NordbeansSite, fixture_html: Callable[[str, str], str]) -> Coffee:
    return parse(site, fixture_html, "detail_19_house_blend.html", "19")


# --------------------------------------------------------------------------- registry


def test_the_adapter_registers_itself() -> None:
    adapter = get_site("nordbeans")
    assert isinstance(adapter, NordbeansSite)
    assert adapter.base_url == "https://www.nordbeans.cz/"
    assert adapter.country == "CZ"
    assert adapter.kind == "roaster"


# --------------------------------------------------------------------------- discovery


def test_the_category_yields_twenty_refs_with_real_urls(refs: list[ProductRef]) -> None:
    assert len(refs) == 20
    ids = [ref.external_id for ref in refs]
    assert len(set(ids)) == len(ids)
    assert {"3626", "3916", "3670", "19"} <= set(ids)
    by_id = {ref.external_id: ref for ref in refs}
    assert by_id["3626"].url == f"{BASE}/bold-deer_z3626/"
    assert all(ref.url.startswith(f"{BASE}/") for ref in refs)
    assert all(external_id_of(ref.url) == ref.external_id for ref in refs)


def test_the_cards_carry_price_origin_and_cup_notes(refs: list[ProductRef]) -> None:
    by_id = {ref.external_id: ref for ref in refs}
    bold_deer = by_id["3626"]
    assert bold_deer.name == "BOLD DEER"
    assert bold_deer.price == 375.0
    assert bold_deer.currency == "CZK"
    assert bold_deer.image_url is not None
    assert bold_deer.image_url.startswith("https://data.nordbeans.cz/data/tmp/")
    assert bold_deer.extra["origin"] == "Uganda"
    assert bold_deer.extra["flavor_notes"] == "marcipán – maliny – švestky"
    assert bold_deer.extra["profile"] == "Espresso"
    assert bold_deer.extra["stock"] == "Skladem"
    assert "ESPRESSO" in bold_deer.extra["flags"]


def test_cards_without_a_product_link_are_skipped(site: NordbeansSite) -> None:
    html = '<div class="catalog"><a class="product-link" href="/clanky/">x</a></div>'
    assert site.parse_listing(html) == []


def test_discover_walks_one_category_per_page_budget(
    fixture_html: Callable[[str, str], str],
) -> None:
    espresso = fixture_html("nordbeans", "list_espresso.html")
    fake = FakeFetcher({ESPRESSO_URL: espresso})
    site = NordbeansSite()
    refs = list(site.discover(cast("PoliteFetcher", fake), max_pages=1))
    assert fake.requested == [ESPRESSO_URL]
    assert len(refs) == 20


def test_discover_deduplicates_across_categories(
    fixture_html: Callable[[str, str], str],
) -> None:
    espresso = fixture_html("nordbeans", "list_espresso.html")
    fake = FakeFetcher({ESPRESSO_URL: espresso, FILTER_URL: espresso})
    site = NordbeansSite()
    refs = list(site.discover(cast("PoliteFetcher", fake), max_pages=2))
    assert fake.requested == [ESPRESSO_URL, FILTER_URL]
    assert len(refs) == 20  # the second category repeats the first, nothing is added


# --------------------------------------------------------------------------- detail pages


def test_gakenke_single_origin(gakenke: Coffee) -> None:
    assert gakenke.name == "Gakenke Honey"
    assert gakenke.external_id == "3984"
    assert (gakenke.price, gakenke.currency, gakenke.weight_g) == (375.0, "CZK", 250)
    assert gakenke.price_per_kg == 1500.0
    assert gakenke.available is True
    assert gakenke.decaf is False
    assert gakenke.origin.country == "BI"
    assert gakenke.origin.region == "Kyanza"
    assert gakenke.origin.farm == "Gatara"
    assert gakenke.origin.washing_station == "Gakenke Washing station"
    assert (gakenke.origin.altitude_min_m, gakenke.origin.altitude_max_m) == (1500, 1500)
    assert gakenke.origin.altitude_raw == "1500 m n.m."
    assert gakenke.origin.variety == ["Red Bourbon"]
    assert gakenke.processing.method is ProcessMethod.HONEY
    assert gakenke.processing.raw == "Honey"
    assert gakenke.roast.profile is RoastProfile.FILTER
    assert gakenke.origin_text == "Burundi"


def test_gakenke_taste_media_and_categories(gakenke: Coffee) -> None:
    assert (gakenke.taste.body, gakenke.taste.acidity) == (3, 3)
    assert gakenke.taste.scale_max == 5
    assert gakenke.taste.flavor_notes == ["višně", "med", "černý rybíz"]
    assert gakenke.taste.tasting_text == "Sladká, Ovocná, Květinová"
    assert gakenke.taste.brewing_methods == [
        "Filtr",
        "Aeropress",
        "Frenchpress",
        "Chemex",
        "Batch brew",
    ]
    assert len(gakenke.images) >= 2
    assert all(image.startswith("https://data.nordbeans.cz/data/tmp/") for image in gakenke.images)
    assert gakenke.categories[:2] == ["Káva", "Filtrovaná káva"]
    assert "NOVINKA" in gakenke.tags


def test_gakenke_variants_carry_both_package_sizes(gakenke: Coffee) -> None:
    by_weight = {variant.weight_g: variant for variant in gakenke.variants}
    assert set(by_weight) == {250, 1000}
    assert by_weight[250].label == "Hmotnost: 250g"
    assert by_weight[250].external_id == "3984_1417"
    assert by_weight[1000].price == 1216.96
    assert by_weight[1000].currency == "CZK"


def test_gakenke_keeps_every_label_in_raw_attributes(gakenke: Coffee) -> None:
    raw = gakenke.raw_attributes
    assert len(raw) >= 5
    assert raw["ZPRACOVÁNÍ"] == "Honey"
    assert raw["ODRŮDA"] == "Red Bourbon"
    assert raw["PŮVOD"] == "Burundi"
    assert raw["ZPRACOVATELSKÝ ZÁVOD"] == "Gakenke Washing station"
    assert raw["INTENZITA TĚLA"] == "Střední"
    assert raw["CODE"] == "1110000000092"
    assert raw["PRODUCER"] == "Nordbeans"
    assert "META_DESCRIPTION" in raw
    assert all(key == key.upper() == key.strip() for key in raw)


def test_house_blend_is_marked_as_a_blend(house_blend: Coffee) -> None:
    assert house_blend.name == "House Blend"
    assert house_blend.species.is_blend is True
    assert house_blend.origin_text == "Brazílie/Vietnam"
    assert house_blend.origin.country == "BR"
    assert house_blend.roast.profile is RoastProfile.ESPRESSO
    assert house_blend.taste.body == 5
    assert house_blend.taste.acidity == 1
    assert house_blend.price == 246.0
    assert house_blend.weight_g == 250
    assert len(house_blend.raw_attributes) >= 5
    assert house_blend.raw_attributes["ŘADA"] == "Sweet City"


# --------------------------------------------------------------------------- edge cases


@pytest.mark.parametrize(
    "name",
    [
        "Kapsle Horizont - 16ks",
        "Výběr baristy: four-pack na espresso",
        "Dárkový poukaz",
    ],
)
def test_non_bean_products_are_skipped(site: NordbeansSite, name: str) -> None:
    assert site.is_ignored(name) is True
    html = f"<html><body><h1>{name}</h1></body></html>"
    assert site.parse_product(html, ProductRef("nordbeans", "1", f"{BASE}/x_z1/")) is None


def test_the_category_lists_capsules_that_parsing_then_drops(
    site: NordbeansSite,
    refs: list[ProductRef],
) -> None:
    kept = [ref for ref in refs if not site.is_ignored(ref.name)]
    assert len(kept) == 15  # 20 cards minus four capsule packs and one four-pack
    assert "3646" not in {ref.external_id for ref in kept}


def test_a_page_without_a_parameter_list_still_parses(site: NordbeansSite) -> None:
    html = '<html><body><h1>Holá káva</h1><p class="price" data-price>250 Kč</p></body></html>'
    coffee = site.parse_product(html, ProductRef("nordbeans", "7", f"{BASE}/hola_z7/"))
    assert coffee is not None
    assert coffee.name == "Holá káva"
    assert coffee.price == 250.0
    assert coffee.currency == "CZK"
    assert coffee.raw_attributes == {}
    assert coffee.available is None
    assert coffee.variants == []


def test_the_sitemap_yields_one_ref_per_product(site: NordbeansSite) -> None:
    xml = (
        '<?xml version="1.0"?><urlset>'
        "<url><loc>https://www.nordbeans.cz/uganda_z3670/</loc></url>"
        "<url><loc>https://www.nordbeans.cz/uganda_z3670/</loc></url>"
        "<url><loc>https://www.nordbeans.cz/o-nas/</loc></url>"
        "</urlset>"
    )
    refs = site.parse_sitemap(xml)
    assert [ref.external_id for ref in refs] == ["3670"]


def test_dash_separated_cup_notes_are_split() -> None:
    assert split_notes("višně – med – černý rybíz") == ["višně", "med", "černý rybíz"]
    assert split_notes("jablko - karamel, kakao") == ["jablko", "karamel", "kakao"]
    assert split_notes(None) == []
    assert split_notes("dark-roasted cocoa") == ["dark-roasted cocoa"]
