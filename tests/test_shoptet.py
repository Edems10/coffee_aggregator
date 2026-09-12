from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from coffee_aggregator import platforms, sites
from coffee_aggregator.http import FetchResult
from coffee_aggregator.models import ProcessMethod, RoastLevel, RoastProfile
from coffee_aggregator.platforms.shoptet import (
    DEFAULT_LABEL_MAP,
    F_PROCESS,
    F_WEIGHT,
    KNOWN_FIELDS,
    ShoptetConfigError,
    ShoptetSite,
    _Labels,
)
from coffee_aggregator.sites import get as get_site
from coffee_aggregator.sites.base import ProductRef
from conftest import FIXTURE_ROOT

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee

KAVYPITEL = "shoptet_kavypitel"
REDFAWN = "shoptet_redfawn"
KP_CATEGORY = "https://www.kavypitel.cz/kava-z-ruznych-koutu-sveta/"
RF_CATEGORY = "https://www.redfawn.sk/kava/"

MINIMAL_TOML = """
platform = "shoptet"
site_id = "tinyroastery"
name = "Tiny Roastery"
country = "SK"
base_url = "https://tiny.sk"
category_urls = ["/zrnkova-kava/"]
currency = "EUR"
"""


class FakeFetcher:
    """Serves canned listing pages without any network."""

    def __init__(self, bodies: dict[str, str]) -> None:
        self.bodies = bodies
        self.requested: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.requested.append(url)
        body = self.bodies.get(url, "<html><body></body></html>")
        return FetchResult(url, url, 200, body, from_cache=False, elapsed_s=0.0)


def as_fetcher(fake: FakeFetcher) -> PoliteFetcher:
    return cast("PoliteFetcher", fake)


@pytest.fixture
def kavypitel() -> ShoptetSite:
    return cast("ShoptetSite", get_site("kavypitel"))


@pytest.fixture
def redfawn() -> ShoptetSite:
    return cast("ShoptetSite", get_site("redfawn"))


def parse(site: ShoptetSite, html: str, external_id: str, url: str) -> Coffee:
    ref = ProductRef(site_id=site.site_id, external_id=external_id, url=url)
    coffee = site.parse_product(html, ref)
    assert coffee is not None
    return coffee


# --- the two configured shops are registered without any Python change -------


def test_both_shops_are_registered_from_toml_alone(
    kavypitel: ShoptetSite,
    redfawn: ShoptetSite,
) -> None:
    assert (kavypitel.kind, kavypitel.country, kavypitel.name) == ("shoptet", "CZ", "Kávy pitel")
    assert (redfawn.kind, redfawn.country, redfawn.name) == ("shoptet", "SK", "Red Fawn")
    assert kavypitel.config.category_urls == [KP_CATEGORY]
    assert redfawn.config.category_urls == [RF_CATEGORY]


# --- discovery ---------------------------------------------------------------


def test_parse_listing_reads_every_card(
    kavypitel: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    refs = kavypitel.parse_listing(fixture_html(KAVYPITEL, "list_page1.html"))
    assert len(refs) == 22
    first = refs[0]
    assert first.external_id == "966"
    assert first.url == (
        "https://www.kavypitel.cz/honduras-250g-zrnkova-kava-cerstve-prazena-kavy-pitel"
        "-kutna-hora-noir-edition/"
    )
    assert first.price == 297.0
    assert first.currency == "CZK"
    assert first.extra["sku"] == "HON/250NOIR"
    assert first.image_url is not None
    assert first.image_url.startswith("https://cdn.myshoptet.com/")
    assert all(ref.url.startswith("https://www.kavypitel.cz/") for ref in refs)
    assert len({ref.external_id for ref in refs}) == len(refs)


def test_parse_listing_ignores_the_recommendation_carousel(
    redfawn: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    """The carousel above the grid repeats products; only ``#products`` counts."""
    refs = redfawn.parse_listing(fixture_html(REDFAWN, "list_page1.html"))
    assert len(refs) == 12
    assert [ref.external_id for ref in refs[:3]] == ["354", "330", "315"]
    assert refs[0].price == 8.0
    assert refs[0].currency == "EUR"


def test_page_url_supports_both_shoptet_pagination_styles(redfawn: ShoptetSite) -> None:
    assert redfawn.page_url(RF_CATEGORY, 1) == RF_CATEGORY
    assert redfawn.page_url(RF_CATEGORY, 2) == f"{RF_CATEGORY}strana-2/"
    redfawn.config.pagination = "query"
    try:
        assert redfawn.page_url(RF_CATEGORY, 3) == f"{RF_CATEGORY}?page=3"
        assert redfawn.page_url(f"{RF_CATEGORY}?a=1", 3) == f"{RF_CATEGORY}?a=1&page=3"
    finally:
        redfawn.config.pagination = "path"


def test_discover_walks_pagination_to_the_second_page(
    redfawn: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    fake = FakeFetcher(
        {
            RF_CATEGORY: fixture_html(REDFAWN, "list_page1.html"),
            f"{RF_CATEGORY}strana-2/": fixture_html(REDFAWN, "list_page2.html"),
        }
    )
    refs = list(redfawn.discover(as_fetcher(fake)))
    assert len(refs) == 14
    assert [ref.external_id for ref in refs[-2:]] == ["483", "464"]
    assert fake.requested == [
        RF_CATEGORY,
        f"{RF_CATEGORY}strana-2/",
        f"{RF_CATEGORY}strana-3/",
    ]


def test_discover_stops_on_an_empty_page(
    kavypitel: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    """Shoptet answers an out-of-range page with a product-less category page."""
    fake = FakeFetcher(
        {
            KP_CATEGORY: fixture_html(KAVYPITEL, "list_page1.html"),
            f"{KP_CATEGORY}strana-2/": fixture_html(KAVYPITEL, "list_page2.html"),
        }
    )
    refs = list(kavypitel.discover(as_fetcher(fake)))
    assert len(refs) == 22
    assert fake.requested == [KP_CATEGORY, f"{KP_CATEGORY}strana-2/"]


def test_discover_stops_when_a_page_repeats_the_previous_one(
    redfawn: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    page1 = fixture_html(REDFAWN, "list_page1.html")
    fake = FakeFetcher({RF_CATEGORY: page1, f"{RF_CATEGORY}strana-2/": page1})
    refs = list(redfawn.discover(as_fetcher(fake)))
    assert len(refs) == 12
    assert fake.requested == [RF_CATEGORY, f"{RF_CATEGORY}strana-2/"]


def test_discover_respects_max_pages(
    redfawn: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    fake = FakeFetcher({RF_CATEGORY: fixture_html(REDFAWN, "list_page1.html")})
    redfawn.max_pages = 1
    try:
        assert len(list(redfawn.discover(as_fetcher(fake)))) == 12
    finally:
        redfawn.max_pages = redfawn.config.max_pages
    assert fake.requested == [RF_CATEGORY]


# --- parsing: the CZ shop, whose parameter table is complete -----------------


def test_parse_kavypitel_single_origin(
    kavypitel: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    coffee = parse(
        kavypitel,
        fixture_html(KAVYPITEL, "detail_brasil.html"),
        "1775",
        "https://www.kavypitel.cz/brasil-kavy-pitel-vyberova-zrnkova-kava--250g-/",
    )
    assert coffee.name == "Brasil Kávy pitel - výběrová zrnková káva (250g)"
    assert coffee.external_id == "1775"
    assert (coffee.price, coffee.currency, coffee.weight_g) == (287.0, "CZK", 250)
    assert coffee.price_per_kg == 1148.0
    assert coffee.available is True
    assert coffee.origin.country == "BR"
    assert coffee.origin.altitude_min_m == 1000
    assert coffee.origin.altitude_max_m == 1300
    assert coffee.origin.variety == ["Mundo Novo"]  # the species prefix is not a cultivar
    assert coffee.origin.harvest == "duben - srpen"
    assert coffee.processing.method is ProcessMethod.NATURAL
    assert coffee.processing.raw == "natural / ruční sběr"
    assert coffee.roast.level is RoastLevel.MEDIUM
    assert coffee.roast.raw == "City Plus"
    assert coffee.species.arabica_pct == 100
    assert coffee.species.is_blend is False
    assert coffee.taste.sca_score == 81.5
    assert coffee.popularity.rating == 5.0
    assert coffee.popularity.review_count == 5
    assert coffee.specialty_grade is True
    assert coffee.origin_text == "region Sul De Minas, Brazílie, Jižní Amerika"
    assert coffee.categories[0] == "Káva z různých koutů světa"
    assert len(coffee.raw_attributes) >= 4
    assert coffee.raw_attributes["STUPEŇ PRAŽENÍ"] == "City Plus"
    assert coffee.raw_attributes["EAN"] == "8594205570946"


def test_unmapped_labels_still_reach_raw_attributes(
    kavypitel: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    coffee = parse(kavypitel, fixture_html(KAVYPITEL, "detail_brasil.html"), "1775", "https://x/")
    assert "MÍSTO PRAŽENÍ" in coffee.raw_attributes
    assert "KVALITA KÁVY" in coffee.raw_attributes
    assert coffee.roast.raw == "City Plus"  # "Místo pražení" never fed the roast


# --- parsing: the SK shop, which states everything in the description --------


def test_parse_redfawn_single_origin(
    redfawn: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    coffee = parse(
        redfawn,
        fixture_html(REDFAWN, "detail_colombia_suukala.html"),
        "351",
        "https://www.redfawn.sk/colombia-suukala/",
    )
    assert coffee.name == "Colombia Suukala Washed"
    assert (coffee.price, coffee.currency, coffee.weight_g) == (14.0, "EUR", 250)
    assert coffee.origin.country == "CO"
    assert coffee.origin.region == "Cauca"
    assert coffee.origin.producer == "Suukala Caficauca"
    assert (coffee.origin.altitude_min_m, coffee.origin.altitude_max_m) == (1800, 2050)
    assert coffee.origin.variety == ["Castillo", "Caturra"]
    assert coffee.processing.method is ProcessMethod.WASHED
    assert coffee.roast.profile is RoastProfile.ESPRESSO
    assert coffee.taste.sca_score == 85.25
    assert coffee.taste.flavor_notes == ["Citrus", "Sušená slivka", "Tmavé kakao", "Lieskovce"]
    assert coffee.species.is_blend is False
    assert len(coffee.raw_attributes) >= 4
    assert coffee.raw_attributes["PÔVOD"] == "Kolumbia · Cauca"
    assert coffee.raw_attributes["GRAMÁŽ"] == "250g, 1000g"


def test_parse_redfawn_blend_claims_no_single_country(
    redfawn: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    coffee = parse(
        redfawn,
        fixture_html(REDFAWN, "detail_red_flag_blend.html"),
        "375",
        "https://www.redfawn.sk/red-flag-espresso-blend/",
    )
    assert coffee.name == "Red Flag Espresso Blend"
    assert coffee.species.is_blend is True
    assert coffee.origin.country is None
    assert coffee.origin.harvest == "2024 / 2025"
    assert coffee.roast.profile is RoastProfile.ESPRESSO
    assert coffee.price == 13.5


def test_variants_pair_each_offer_with_its_weight(
    redfawn: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    coffee = parse(
        redfawn, fixture_html(REDFAWN, "detail_red_flag_blend.html"), "375", "https://x/"
    )
    assert [variant.weight_g for variant in coffee.variants] == [250, 500, 1000]
    assert [variant.price for variant in coffee.variants] == [13.5, 25.0, 47.0]
    assert [variant.external_id for variant in coffee.variants] == ["375/250", "375/500", "375/100"]
    assert all(variant.currency == "EUR" for variant in coffee.variants)
    assert all(variant.available for variant in coffee.variants)


def test_related_products_never_leak_into_the_parsed_coffee(
    kavypitel: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    """``.p-detail`` also contains related-product cards with the same microdata."""
    coffee = parse(kavypitel, fixture_html(KAVYPITEL, "detail_brasil.html"), "1775", "https://x/")
    assert coffee.price == 287.0
    assert coffee.original_price is None
    assert coffee.variants == []
    expected_image = (
        "https://cdn.myshoptet.com/usr/www.kavypitel.cz/user/shop/big/1775_brazilska-zrnkova"
        "-kava-brazilie-kavy-pitel-brazil-250g-f1.jpg?ff=1&x=1024&y=768&q=85&ts=69cbfd8c"
        "&sg=161563f2"
    )
    assert coffee.images == [expected_image]


def test_non_coffee_products_are_skipped(redfawn: ShoptetSite) -> None:
    assert redfawn.is_ignored("Hacienda Sonora - Cascara") is True
    assert redfawn.is_ignored("Mlynček Nuttii Gosey") is True
    assert redfawn.is_ignored("Colombia Suukala Washed") is False


def test_a_page_without_a_product_block_parses_to_none(redfawn: ShoptetSite) -> None:
    ref = ProductRef(site_id="redfawn", external_id="1", url="https://www.redfawn.sk/x/")
    assert redfawn.parse_product("<html><body>404</body></html>", ref) is None


# --- configuration -----------------------------------------------------------


def _write(tmp_path: Path, body: str, name: str = "shop.toml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_label_map_override_maps_a_label_the_defaults_do_not_know(
    tmp_path: Path,
    fixture_html: Callable[[str, str], str],
) -> None:
    """``Bližší určení`` is this shop's own word for the region."""
    base = """
platform = "shoptet"
site_id = "kavypitel-probe"
name = "probe"
country = "CZ"
base_url = "https://www.kavypitel.cz/"
category_urls = ["/kava-z-ruznych-koutu-sveta/"]
"""
    html = fixture_html(KAVYPITEL, "detail_brasil.html")
    plain = cast("ShoptetSite", platforms.build_from_config(_write(tmp_path, base, "plain.toml")))
    assert parse(plain, html, "1775", "https://x/").origin.region is None

    override = base + '\n[label_map]\n"Bližší určení" = "region"\n'
    mapped = cast(
        "ShoptetSite",
        platforms.build_from_config(_write(tmp_path, override, "mapped.toml")),
    )
    assert parse(mapped, html, "1775", "https://x/").origin.region == "Brasil, Sul De Minas"
    # the label is in raw_attributes either way
    assert "BLIŽŠÍ URČENÍ" in parse(plain, html, "1775", "https://x/").raw_attributes


def test_a_minimal_toml_yields_a_working_adapter(tmp_path: Path) -> None:
    """Adding a Shoptet shop needs no Python at all."""
    adapter = platforms.build_from_config(_write(tmp_path, MINIMAL_TOML))
    assert isinstance(adapter, ShoptetSite)
    assert adapter.site_id == "tinyroastery"
    assert adapter.base_url == "https://tiny.sk/"
    assert adapter.config.category_urls == ["https://tiny.sk/zrnkova-kava/"]
    assert adapter.page_url("https://tiny.sk/zrnkova-kava/", 2) == (
        "https://tiny.sk/zrnkova-kava/strana-2/"
    )

    card = """
    <html><body><div id="products">
      <div class="p" data-micro="product" data-micro-product-id="7">
        <a class="name" href="/kolumbia-la-paz/"><span data-micro="name">Kolumbia La Paz</span></a>
        <div data-micro="offer" data-micro-price="12.50" data-micro-price-currency="EUR"></div>
      </div>
    </div></body></html>
    """
    fake = FakeFetcher({"https://tiny.sk/zrnkova-kava/": card})
    refs = list(adapter.discover(as_fetcher(fake)))
    assert len(refs) == 1
    assert refs[0].url == "https://tiny.sk/kolumbia-la-paz/"
    assert refs[0].external_id == "7"
    assert refs[0].price == 12.50


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ('platform = "shoptet"\nname = "x"\ncountry = "CZ"\nbase_url = "https://x/"\n', "site_id"),
        (
            MINIMAL_TOML.replace('category_urls = ["/zrnkova-kava/"]', "category_urls = []"),
            "category_urls",
        ),
        (MINIMAL_TOML.replace('country = "SK"', 'country = "PL"'), "country"),
        (MINIMAL_TOML + 'pagination = "cursor"\n', "pagination"),
    ],
)
def test_a_broken_toml_is_rejected_with_an_actionable_message(
    tmp_path: Path,
    body: str,
    problem: str,
) -> None:
    with pytest.raises(ShoptetConfigError) as excinfo:
        platforms.build_from_config(_write(tmp_path, body))
    assert problem in str(excinfo.value)


# --- item 13: a label is never claimed by a substring of it ------------------


def _mapped(label: str, value: str) -> dict[str, str]:
    labels = _Labels()
    labels.add(label, value, DEFAULT_LABEL_MAP)
    return labels.by_field


@pytest.mark.parametrize(
    ("label", "value"),
    [
        # "balení" hides inside a sentence about packaging material
        ("BALENÍ KÁVY ZNAČKY KÁVY PITEL", "veškerou kávu balíme do kvalitních sáčků"),
        ("Postup balení kávy značky Kávy pitel", "Po pražení se z kávy uvolňuje plyn"),
        # "zpracování" hides inside a sentence about order handling
        ("Popis zpracování objednávky", "Objednávky zpracováváme v pracovní dny do 24 hodin"),
        # and "1 kg" is a shipping threshold, not a package size
        ("Objednávky nad 1 kg zasíláme zdarma", "Doprava zdarma po celé ČR"),
    ],
)
def test_a_sentence_shaped_label_never_feeds_a_typed_field(label: str, value: str) -> None:
    assert _mapped(label, value) == {}


def test_the_label_still_reaches_raw_attributes(
    kavypitel: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    coffee = parse(kavypitel, fixture_html(KAVYPITEL, "detail_brasil.html"), "1775", "https://x/")
    assert "BALENÍ KÁVY ZNAČKY KÁVY PITEL" in coffee.raw_attributes
    assert coffee.weight_g == 250  # not the 1000 g a stray "1 kg" would have claimed


def test_a_short_label_still_matches_on_whole_words() -> None:
    assert _mapped("Stupeň pražení", "City Plus") == {"roast": "City Plus"}
    assert _mapped("Q Score", "85.25 pts") == {"sca_score": "85.25 pts"}


@pytest.mark.parametrize(
    ("label", "value", "field_name"),
    [
        ("Gramáž", "3 g", F_WEIGHT),  # too light to be a bag of beans
        ("Gramáž", "25 kg", F_WEIGHT),  # a sack, not a retail package
        (
            "Zpracování",
            "káva se zpracovává tradiční metodou na našich farmách v Brazílii",
            F_PROCESS,
        ),
    ],
)
def test_an_implausible_value_never_feeds_its_field(
    label: str,
    value: str,
    field_name: str,
) -> None:
    assert field_name not in _mapped(label, value)
    assert _Labels().raw == {}  # …but the raw dictionary of a real page keeps it


def test_a_country_label_that_names_no_country_is_not_used() -> None:
    assert _mapped("Původ", "z našich farem") == {}
    assert _mapped("Původ", "Brazílie") == {"country": "Brazílie"}


# --- item 14: a label_map typo is a startup error ----------------------------


def test_an_unknown_label_map_field_is_rejected(tmp_path: Path) -> None:
    body = MINIMAL_TOML + '\n[label_map]\n"Bližší určení" = "regoin"\n'
    with pytest.raises(ShoptetConfigError) as excinfo:
        platforms.build_from_config(_write(tmp_path, body))
    message = str(excinfo.value)
    assert "Bližší určení" in message
    assert "regoin" in message
    assert "region" in message  # the valid fields are listed


def test_every_default_label_points_at_a_known_field() -> None:
    assert set(DEFAULT_LABEL_MAP.values()) <= KNOWN_FIELDS


# --- item 15: the grind axis is a brewing statement --------------------------


def test_the_grind_axis_feeds_brewing_and_the_roast_profile(
    kavypitel: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    coffee = parse(kavypitel, fixture_html(KAVYPITEL, "detail_brasil.html"), "1775", "https://x/")
    assert coffee.taste.brewing_methods == ["Espresso", "Filtr", "Turek", "Moka", "French press"]
    assert coffee.roast.profile is not RoastProfile.UNKNOWN
    assert coffee.raw_attributes["KÁVU NAMELTE NA"].startswith("Espresso +10")


# --- item 16: a bar stated in words -----------------------------------------


def test_sensory_words_become_bars_when_no_points_are_drawn(tmp_path: Path) -> None:
    adapter = cast("ShoptetSite", platforms.build_from_config(_write(tmp_path, MINIMAL_TOML)))
    page = """
    <html><body><div class="p-detail"><h1>Etiópia Guji</h1>
      <table class="detail-parameters">
        <tr><th>Telo</th><td>vysoké</td></tr>
        <tr><th>Acidita</th><td>stredná</td></tr>
        <tr><th>Horkosť</th><td>nízka</td></tr>
        <tr><th>Sladkosť</th><td>veľmi vysoká</td></tr>
      </table>
    </div></body></html>
    """
    coffee = parse(adapter, page, "1", "https://tiny.sk/etiopia/")
    taste = coffee.taste
    assert (taste.body, taste.acidity, taste.bitterness, taste.sweetness) == (4, 3, 2, 5)
    assert taste.scale_max == 5


def test_drawn_points_still_win_over_words(tmp_path: Path) -> None:
    adapter = cast("ShoptetSite", platforms.build_from_config(_write(tmp_path, MINIMAL_TOML)))
    page = """
    <html><body><div class="p-detail"><h1>Etiópia Guji</h1>
      <table class="detail-parameters"><tr><th>Telo</th><td>3 z 5</td></tr></table>
    </div></body></html>
    """
    assert parse(adapter, page, "1", "https://tiny.sk/e/").taste.body == 3


# --- item 18: a blend keeps every process it is made of ----------------------


def test_a_blend_records_every_process_it_names(
    redfawn: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    coffee = parse(
        redfawn, fixture_html(REDFAWN, "detail_red_flag_blend.html"), "375", "https://x/"
    )
    assert coffee.processing.raw == "Washed · Natural"
    assert coffee.processing.method is ProcessMethod.MIXED
    assert coffee.processing.methods == [ProcessMethod.WASHED, ProcessMethod.NATURAL]
    assert coffee.to_record()["process_methods"] == ["washed", "natural"]


# --- item 19: de-duplication never ends a category's pagination --------------


def _grid(ids: list[int]) -> str:
    cards = "".join(
        f'<div class="p" data-micro="product" data-micro-product-id="{identifier}">'
        f'<a class="name" href="/p/{identifier}/"><span data-micro="name">P{identifier}</span></a>'
        "</div>"
        for identifier in ids
    )
    return f'<html><body><div id="products">{cards}</div></body></html>'


def test_two_overlapping_categories_are_both_paged_to_the_end(tmp_path: Path) -> None:
    """Category B's first page repeats A entirely; B's second page must still load."""
    body = MINIMAL_TOML.replace(
        'category_urls = ["/zrnkova-kava/"]',
        'category_urls = ["/a/", "/b/"]',
    )
    adapter = cast("ShoptetSite", platforms.build_from_config(_write(tmp_path, body)))
    fake = FakeFetcher(
        {
            "https://tiny.sk/a/": _grid([1, 2]),
            "https://tiny.sk/a/strana-2/": _grid([3]),
            "https://tiny.sk/b/": _grid([1, 2]),
            "https://tiny.sk/b/strana-2/": _grid([4]),
        }
    )
    refs = list(adapter.discover(as_fetcher(fake)))

    assert [ref.external_id for ref in refs] == ["1", "2", "3", "4"]
    assert fake.requested == [
        "https://tiny.sk/a/",
        "https://tiny.sk/a/strana-2/",
        "https://tiny.sk/a/strana-3/",
        "https://tiny.sk/b/",
        "https://tiny.sk/b/strana-2/",
        "https://tiny.sk/b/strana-3/",
    ]


def test_a_repeated_page_still_ends_the_walk(tmp_path: Path) -> None:
    adapter = cast("ShoptetSite", platforms.build_from_config(_write(tmp_path, MINIMAL_TOML)))
    fake = FakeFetcher(
        {
            "https://tiny.sk/zrnkova-kava/": _grid([1, 2]),
            "https://tiny.sk/zrnkova-kava/strana-2/": _grid([2, 1]),
        }
    )
    assert [ref.external_id for ref in adapter.discover(as_fetcher(fake))] == ["1", "2"]
    assert len(fake.requested) == 2


# --- item 20: page metadata is kept -----------------------------------------


def test_page_metadata_reaches_raw_attributes(
    redfawn: ShoptetSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    coffee = parse(
        redfawn, fixture_html(REDFAWN, "detail_colombia_suukala.html"), "351", "https://x/"
    )
    assert coffee.raw_attributes["OG_TITLE"]
    assert "Colombia Suukala" in coffee.raw_attributes["META_DESCRIPTION"]
    assert coffee.raw_attributes["OG_DESCRIPTION"]


# --- item 22: every shipped config parses its own saved pages ----------------

SHIPPED = ["conceptcoffee", "kavypitel", "redfawn", "valasska"]


def _fixture_dir(site_id: str) -> Path:
    return FIXTURE_ROOT / f"shoptet_{site_id}"


@pytest.mark.parametrize("site_id", SHIPPED)
def test_every_shipped_config_reads_its_listing(site_id: str) -> None:
    site = cast("ShoptetSite", get_site(site_id))
    refs = site.parse_listing((_fixture_dir(site_id) / "list_page1.html").read_text("utf-8"))

    assert refs, f"{site_id} found no products on its saved listing"
    assert all(ref.url.startswith(site.base_url) for ref in refs)
    assert all(ref.external_id for ref in refs)
    assert len({ref.external_id for ref in refs}) == len(refs)


@pytest.mark.parametrize("site_id", SHIPPED)
def test_every_shipped_config_reads_a_detail_page(site_id: str) -> None:
    site = cast("ShoptetSite", get_site(site_id))
    path = min(_fixture_dir(site_id).glob("detail_*.html"))
    coffee = parse(site, path.read_text("utf-8"), "1", f"{site.base_url}p/")

    assert coffee.name
    assert coffee.price is not None
    assert coffee.currency
    assert coffee.weight_g is not None
    assert coffee.price_per_kg is not None
    assert coffee.origin.country is None or len(coffee.origin.country) == 2
    assert coffee.processing.method in set(ProcessMethod)
    assert all(method in set(ProcessMethod) for method in coffee.processing.methods)
    assert coffee.roast.level in set(RoastLevel)
    assert coffee.roast.profile in set(RoastProfile)
    assert len(coffee.raw_attributes) >= 4, coffee.raw_attributes


def test_every_shipped_config_is_registered() -> None:
    registered = {adapter.site_id for adapter in sites.all_sites()}
    assert set(SHIPPED) <= registered
