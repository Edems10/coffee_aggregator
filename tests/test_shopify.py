from __future__ import annotations

import json
import tomllib
from typing import TYPE_CHECKING, cast

import pytest

from coffee_aggregator import platforms, sites
from coffee_aggregator.http import FetchResult, parse_robots
from coffee_aggregator.labels import KNOWN_FIELDS
from coffee_aggregator.models import ProcessMethod, RoastLevel, RoastProfile
from coffee_aggregator.platforms.shopify import (
    DEFAULT_LABEL_MAP,
    ShopifyConfigError,
    ShopifySite,
    _amount,
)
from coffee_aggregator.sites import CONFIG_DIR
from coffee_aggregator.sites import get as get_site
from conftest import FIXTURE_ROOT

if TYPE_CHECKING:
    from pathlib import Path

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee

PENGUIN = "shopify_penguincoffee"
COFFIA = "shopify_coffia"
PAGE_FILE = "collection_products_page1.json"
#: Written as a code point: a literal one in the source is invisible to a reader.
ZERO_WIDTH_SPACE = chr(0x200B)

MINIMAL_TOML = """
platform = "shopify"
site_id = "tinyroastery"
name = "Tiny Roastery"
country = "SK"
base_url = "https://tiny.sk"
currency = "EUR"
collections = ["kava"]
"""


class FakeFetcher:
    """Serves canned products.json pages without any network."""

    def __init__(self, bodies: dict[str, str]) -> None:
        self.bodies = bodies
        self.requested: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.requested.append(url)
        body = self.bodies.get(url, '{"products": []}')
        return FetchResult(url, url, 200, body, from_cache=False, elapsed_s=0.0)


def as_fetcher(fake: FakeFetcher) -> PoliteFetcher:
    return cast("PoliteFetcher", fake)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "shop.toml"
    path.write_text(body, "utf-8")
    return path


def _adapter(tmp_path: Path, body: str = MINIMAL_TOML) -> ShopifySite:
    return cast("ShopifySite", platforms.build_from_config(_write(tmp_path, body)))


def _items(site_id: str) -> list[dict[str, object]]:
    text = (FIXTURE_ROOT / f"shopify_{site_id}" / PAGE_FILE).read_text("utf-8")
    return cast("list[dict[str, object]]", json.loads(text)["products"])


def _by_handle(site_id: str, handle: str) -> dict[str, object]:
    return next(item for item in _items(site_id) if item["handle"] == handle)


def parse(site: ShopifySite, item: dict[str, object]) -> Coffee:
    ref = site._ref(item)
    assert ref is not None
    coffee = site.parse_product("", ref)
    assert coffee is not None
    return coffee


@pytest.fixture
def penguincoffee() -> ShopifySite:
    return cast("ShopifySite", get_site("penguincoffee"))


@pytest.fixture
def coffia() -> ShopifySite:
    return cast("ShopifySite", get_site("coffia"))


# --- both shops are registered from TOML alone, with no Python change ---------


def test_the_shipped_shops_are_registered_from_toml_alone(
    penguincoffee: ShopifySite,
    coffia: ShopifySite,
) -> None:
    assert (penguincoffee.kind, penguincoffee.country) == ("shopify", "CZ")
    assert (coffia.kind, coffia.country) == ("shopify", "SK")
    assert penguincoffee.config.collections == ["kava"]
    assert coffia.config.collections == ["vsetky-kavy", "dark-roast"]
    assert platforms.builder_target("shopify") == "coffee_aggregator.platforms.shopify:build"


# --- configuration validation ------------------------------------------------


def test_an_unknown_label_map_field_is_rejected(tmp_path: Path) -> None:
    body = MINIMAL_TOML + '\n[label_map]\n"CUPSCORE" = "cupping_score"\n'
    with pytest.raises(ShopifyConfigError) as excinfo:
        platforms.build_from_config(_write(tmp_path, body))
    message = str(excinfo.value)
    assert "CUPSCORE" in message
    assert "cupping_score" in message
    assert "sca_score" in message  # the valid fields are listed


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ('platform = "shopify"\nname = "x"\ncountry = "SK"\nbase_url = "https://x/"\n', "site_id"),
        (MINIMAL_TOML.replace('country = "SK"', 'country = "PL"'), "country"),
        # products.json names no currency anywhere, so the shop must state it.
        (MINIMAL_TOML.replace('currency = "EUR"\n', ""), "currency"),
    ],
)
def test_a_broken_toml_is_rejected_with_an_actionable_message(
    tmp_path: Path,
    body: str,
    problem: str,
) -> None:
    with pytest.raises(ShopifyConfigError) as excinfo:
        platforms.build_from_config(_write(tmp_path, body))
    assert problem in str(excinfo.value)


def test_every_default_label_points_at_a_known_field() -> None:
    assert set(DEFAULT_LABEL_MAP.values()) <= KNOWN_FIELDS


def test_the_sources_follow_the_configuration(tmp_path: Path) -> None:
    assert _adapter(tmp_path).sources() == ["collections/kava/products.json"]
    whole = _adapter(tmp_path, MINIMAL_TOML.replace('collections = ["kava"]', ""))
    assert whole.sources() == ["products.json"]
    pinned = _adapter(tmp_path, MINIMAL_TOML + '\nproducts_path = "/collections/x/products.json"\n')
    assert pinned.sources() == ["collections/x/products.json"]


# --- discovery: pagination stops on an empty page ----------------------------


def _page(ids: list[int]) -> str:
    return json.dumps(
        {
            "products": [
                {
                    "id": identifier,
                    "title": f"Káva {identifier}",
                    "handle": f"kava-{identifier}",
                    "variants": [{"id": identifier * 10, "title": "250g", "price": "12.50"}],
                }
                for identifier in ids
            ]
        }
    )


def _url(page: int) -> str:
    return f"https://tiny.sk/collections/kava/products.json?limit=250&page={page}"


def test_discovery_stops_on_the_first_empty_page(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    fake = FakeFetcher({_url(1): _page([1, 2]), _url(2): _page([3]), _url(3): '{"products": []}'})

    refs = list(adapter.discover(as_fetcher(fake)))

    assert [ref.external_id for ref in refs] == ["1", "2", "3"]
    assert fake.requested == [_url(1), _url(2), _url(3)]


def test_discovery_never_trusts_a_count(tmp_path: Path) -> None:
    """A collection's products_count is a cached number that disagrees with the
    JSON on both shipped shops (kava says 42 and serves 24), and the only real
    total is a header FetchResult does not expose. Only an empty page stops it."""
    adapter = _adapter(tmp_path)
    fake = FakeFetcher({_url(1): _page([1]), _url(2): _page([2]), _url(3): '{"products": []}'})

    assert [ref.external_id for ref in adapter.discover(as_fetcher(fake))] == ["1", "2"]


def test_an_unreadable_page_stops_the_walk_instead_of_crashing(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    fake = FakeFetcher({_url(1): _page([1]), _url(2): "<!DOCTYPE html><html>404</html>"})

    assert [ref.external_id for ref in adapter.discover(as_fetcher(fake))] == ["1"]
    assert fake.requested == [_url(1), _url(2)]


def test_discovery_respects_max_pages(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    fake = FakeFetcher({_url(1): _page([1]), _url(2): _page([2]), _url(3): _page([3])})

    refs = list(adapter.discover(as_fetcher(fake), max_pages=2))

    assert [ref.external_id for ref in refs] == ["1", "2"]
    assert fake.requested == [_url(1), _url(2)]


def test_a_product_in_two_collections_is_yielded_once(tmp_path: Path) -> None:
    body = MINIMAL_TOML.replace('collections = ["kava"]', 'collections = ["kava", "espresso"]')
    adapter = _adapter(tmp_path, body)
    other = "https://tiny.sk/collections/espresso/products.json?limit=250&page=1"
    fake = FakeFetcher({_url(1): _page([1, 2]), other: _page([2, 3])})

    assert [ref.external_id for ref in adapter.discover(as_fetcher(fake))] == ["1", "2", "3"]


def test_discovery_carries_the_payload_so_no_detail_page_is_fetched(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    fake = FakeFetcher({_url(1): _page([1])})

    ref = next(iter(adapter.discover(as_fetcher(fake))))

    assert ref.payload is not None
    assert json.loads(ref.payload)["id"] == 1
    assert ref.url == "https://tiny.sk/products/kava-1"
    assert (ref.price, ref.currency) == (12.5, "EUR")
    assert all("/products/kava-" not in url for url in fake.requested)


def test_the_ignore_list_drops_a_product_before_it_costs_anything(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, MINIMAL_TOML + '\nignore = ["darcek"]\n')
    page = json.dumps(
        {
            "products": [
                {"id": 1, "title": "Darčeková karta", "handle": "darcek", "variants": []},
                {"id": 2, "title": "Kolumbia Huila", "handle": "huila", "variants": []},
                {"id": 3, "title": "Cascara čaj", "handle": "cascara", "variants": []},
            ]
        }
    )
    fake = FakeFetcher({_url(1): page})

    assert [ref.external_id for ref in adapter.discover(as_fetcher(fake))] == ["2"]


# --- prices are plain decimal strings, not minor units -----------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("389.00", 389.0),  # a Czech shop quoting whole crowns with a .00 tail
        ("14.50", 14.5),  # a Slovak shop quoting euros
        ("1250.00", 1250.0),  # four figures, and still not 12.50
        ("235950.00", 235950.0),
        ("0.00", None),  # a gift card or a placeholder, never a free bag
        ("", None),
        (None, None),
    ],
)
def test_a_shopify_price_needs_no_minor_unit(raw: str | None, expected: float | None) -> None:
    assert _amount(raw) == expected


def test_a_czech_shop_is_not_priced_a_hundredfold_low(penguincoffee: ShopifySite) -> None:
    coffee = parse(penguincoffee, _by_handle("penguincoffee", "peru-trabaconas"))

    assert coffee.name == "Peru Trabaconas"
    assert (coffee.price, coffee.currency, coffee.weight_g) == (350.0, "CZK", 250)
    assert coffee.price_per_kg == 1400.0
    assert coffee.url == "https://penguincoffee.cz/products/peru-trabaconas"


def test_a_slovak_shop_prices_in_euros(coffia: ShopifySite) -> None:
    coffee = parse(coffia, _by_handle("coffia", "rwanda-mushonyi-natural-vyberova-kava"))

    assert coffee.name == "Rwanda Mushonyi NATURAL - výberová káva"
    assert (coffee.price, coffee.currency, coffee.weight_g) == (14.5, "EUR", 250)
    assert coffee.price_per_kg == 58.0


# --- weights: grams first, the variant title second --------------------------


def test_the_weight_comes_from_the_grams_field(penguincoffee: ShopifySite) -> None:
    coffee = parse(penguincoffee, _by_handle("penguincoffee", "cuba-serrano-lavado-dark-edition"))

    assert [variant.weight_g for variant in coffee.variants] == [250, 500, 1000]
    assert coffee.weight_g == 250  # the cheapest variant carries the headline price


def test_a_variant_without_grams_falls_back_to_its_title(penguincoffee: ShopifySite) -> None:
    """The older penguincoffee.cz products leave grams at 0; coffia.sk always does."""
    item = _by_handle("penguincoffee", "trenggiling-indonesia")
    entries = cast("list[dict[str, object]]", item["variants"])
    assert all(entry["grams"] == 0 for entry in entries)

    coffee = parse(penguincoffee, item)

    assert [(v.label, v.weight_g) for v in coffee.variants] == [
        ("250g", 250),
        ("500g", 500),
        ("1000g", 1000),
    ]
    assert (coffee.price, coffee.weight_g) == (305.0, 250)


def test_a_kilo_written_as_1kg_is_read_as_a_kilo(coffia: ShopifySite) -> None:
    coffee = parse(coffia, _by_handle("coffia", "las-perlitas-1"))

    assert ("Espresso 1kg", 1000) in [(v.label, v.weight_g) for v in coffee.variants]


# --- variants carry their own price, weight and availability -----------------


def test_every_variant_keeps_its_own_price(coffia: ShopifySite) -> None:
    """Unlike the WooCommerce Store API, Shopify states the whole price ladder."""
    coffee = parse(coffia, _by_handle("coffia", "rwanda-mushonyi-natural-vyberova-kava"))

    assert [(v.label, v.weight_g, v.price, v.available) for v in coffee.variants] == [
        ("Filter 250g", 250, 14.5, False),
        ("Filter 500g", 500, 26.0, False),
        ("Filter 1000g", 1000, 50.0, False),
        ("Espresso 250g", 250, 14.5, True),
        ("Espresso 500g", 500, 26.0, True),
        ("Espresso 1000g", 1000, 50.0, True),
    ]
    assert all(variant.currency == "EUR" for variant in coffee.variants)
    assert all(variant.external_id for variant in coffee.variants)
    # One brewing style is sold out and the other is not, so the product is not.
    assert coffee.available is True
    # The variant axis is also the only place this shop states a roast profile.
    assert coffee.roast.profile is RoastProfile.OMNI


# --- body_html label lines reach the typed fields ----------------------------


def test_a_pipe_joined_passport_line_feeds_every_field(penguincoffee: ShopifySite) -> None:
    """penguincoffee.cz writes the whole passport as one line of four facts."""
    coffee = parse(penguincoffee, _by_handle("penguincoffee", "cuba-serrano-lavado-dark-edition"))

    assert coffee.origin.country == "CU"
    assert coffee.origin.region == "Sierra Maestra, Kuba"
    assert (coffee.origin.altitude_min_m, coffee.origin.altitude_max_m) == (1000, 2000)
    assert coffee.origin.variety == ["Typica"]
    assert coffee.processing.method is ProcessMethod.WASHED
    assert coffee.raw_attributes["ZPRACOVÁNÍ"] == "praná"
    assert coffee.raw_attributes["NADMOŘSKÁ VÝŠKA"] == "1000 - 2000 m n. m."


def test_ordinary_label_lines_feed_the_same_fields(coffia: ShopifySite) -> None:
    coffee = parse(coffia, _by_handle("coffia", "rwanda-mushonyi-natural-vyberova-kava"))

    assert coffee.origin.country == "RW"
    assert coffee.origin.farm == "Small Holder Farmers, RWACOF"
    assert (coffee.origin.altitude_min_m, coffee.origin.altitude_max_m) == (1600, 1950)
    assert coffee.origin.variety == ["Red Bourbon"]
    assert coffee.processing.method is ProcessMethod.NATURAL
    assert coffee.taste.sca_score == 87.0
    assert coffee.specialty_grade is True
    assert coffee.taste.flavor_notes == [
        "hrozienka",
        "tmavá čokoláda",
        "broskyňa",
        "sušené slivky",
        "hruška",
    ]


def test_a_colon_less_fact_row_is_read_too(coffia: ShopifySite) -> None:
    """ "Výška 1700-1800" is a parameter row by layout; the label map decides."""
    coffee = parse(coffia, _by_handle("coffia", "los-cipreses"))

    assert (coffee.origin.altitude_min_m, coffee.origin.altitude_max_m) == (1700, 1800)
    assert coffee.origin.variety == ["Villalobos"]
    assert coffee.processing.method is ProcessMethod.ANAEROBIC
    # The shortest label wins a fuzzy match, so the farm keeps its own name.
    assert coffee.origin.farm == "La Isabel, West Valley, Kostarika"


def test_a_heading_takes_the_line_under_it_as_its_value(coffia: ShopifySite) -> None:
    """<p><strong>Chuťový profil</strong></p><p>Jablko, maliny, …</p>"""
    coffee = parse(coffia, _by_handle("coffia", "los-cipreses"))

    assert coffee.taste.flavor_notes == ["Jablko", "maliny", "hnedý cukor", "karamel", "kakao"]


def test_the_shops_own_spelling_comes_from_its_label_map(coffia: ShopifySite) -> None:
    """ "CUPSCORE" is coffia.sk's own word; the shared vocabulary knows "cupping score"."""
    assert coffia.config.label_map["cupscore"] == "sca_score"

    coffee = parse(coffia, _by_handle("coffia", "las-perlitas-1"))

    assert coffee.taste.sca_score == 87.0
    assert coffee.raw_attributes["CUPSCORE"] == "87+"


def test_editor_debris_never_reaches_the_database(coffia: ShopifySite) -> None:
    """The shop's rich-text editor leaves zero-width spaces inside its values."""
    item = _by_handle("coffia", "hacienda-sonora-kostarika")
    assert "\u200b" in str(item["body_html"])

    coffee = parse(coffia, item)

    assert coffee.origin.variety == ["Caturra"]
    assert coffee.origin.producer == "Diego Guardia"
    assert not any("\u200b" in value for value in coffee.raw_attributes.values())


def test_the_vendor_becomes_a_brand_attribute(coffia: ShopifySite) -> None:
    coffee = parse(coffia, _by_handle("coffia", "castanhas"))

    assert coffee.raw_attributes["BRAND"] == "COFFIA"
    assert coffee.origin.washing_station == "The Cocatrel Cooperative"


def test_tags_and_the_product_type_become_categories(penguincoffee: ShopifySite) -> None:
    coffee = parse(penguincoffee, _by_handle("penguincoffee", "colombia-decaf"))

    assert coffee.tags == ["Decaf", "hořká", "na espresso", "na filtr", "sladká"]
    assert coffee.categories[0] == "Jednodruhová Káva"
    assert coffee.decaf is True  # the "Decaf" tag is what says so
    assert coffee.roast.profile is RoastProfile.OMNI  # "na espresso" + "na filtr"
    assert coffee.images
    assert all(url.startswith("https://cdn.shopify.com/") for url in coffee.images)


def test_an_app_written_tag_is_not_a_tag(coffia: ShopifySite) -> None:
    item = _by_handle("coffia", "rwanda-mushonyi-natural-vyberova-kava")
    assert any(str(tag).startswith("__with:") for tag in cast("list[str]", item["tags"]))

    assert parse(coffia, item).tags == []


# --- robots.txt gates the JSON endpoints exactly like a page -----------------


@pytest.mark.parametrize("site_id", ["penguincoffee", "coffia"])
def test_robots_txt_allows_every_url_a_crawl_asks_for(site_id: str) -> None:
    """Read through the fetcher's own parser, which is what gates a real crawl.

    Shopify's stock robots.txt disallows only the transactional surfaces
    (/checkout, /cart.js, /admin, /account); the JSON endpoints are never named,
    so the crawl stays inside what the file allows.
    """
    site = cast("ShopifySite", get_site(site_id))
    parser = parse_robots((FIXTURE_ROOT / f"shopify_{site_id}" / "robots.txt").read_text("utf-8"))

    for source in site.sources():
        assert parser.can_fetch("coffee-aggregator", site.page_url(source, 1))
    assert parser.can_fetch("coffee-aggregator", site.collections_url())
    assert parser.can_fetch("coffee-aggregator", f"{site.base_url}products.json")


# --- every shipped shopify config parses its own saved page ------------------


def _shopify_config_ids() -> list[str]:
    found: list[str] = []
    for path in sorted(CONFIG_DIR.glob("*.toml")):
        with path.open("rb") as handle:
            config = tomllib.load(handle)
        if str(config.get("platform", "")).lower() == "shopify":
            found.append(str(config["site_id"]))
    return found


SHIPPED = _shopify_config_ids()


#: Most shops are onboarded by writing a config and validating it against the
#: live shop, so only the few that exercise a parsing path of their own keep a
#: saved page here. tests/test_site_configs.py is what checks every config.
WITH_FIXTURES = [name for name in SHIPPED if (FIXTURE_ROOT / f"shopify_{name}").is_dir()]


def test_some_shops_still_keep_a_saved_page() -> None:
    """The saved pages are the regression net under the parser; never let it empty."""
    assert len(WITH_FIXTURES) >= 2


@pytest.mark.parametrize("site_id", SHIPPED)
def test_every_shipped_config_is_registered(site_id: str) -> None:
    assert site_id in {adapter.site_id for adapter in sites.all_sites()}


@pytest.mark.parametrize("site_id", WITH_FIXTURES)
def test_every_shipped_config_reads_its_saved_page(site_id: str) -> None:
    site = cast("ShopifySite", get_site(site_id))

    coffees = [parse(site, item) for item in _items(site_id)]

    assert len(coffees) == 5
    for coffee in coffees:
        assert coffee.name
        assert coffee.price is not None
        assert coffee.currency == site.config.currency
        assert coffee.weight_g is not None
        assert coffee.price_per_kg is not None
        assert coffee.url.startswith(f"{site.base_url}products/")
        assert coffee.variants
        assert all(variant.price is not None for variant in coffee.variants)
        assert coffee.origin.country is None or len(coffee.origin.country) == 2
        assert coffee.processing.method in set(ProcessMethod)
        assert coffee.roast.level in set(RoastLevel)
        assert coffee.roast.profile in set(RoastProfile)
        assert coffee.raw_attributes["BRAND"]
        assert coffee.images


@pytest.mark.parametrize("site_id", WITH_FIXTURES)
def test_every_shipped_config_names_collections_its_shop_publishes(site_id: str) -> None:
    site = cast("ShopifySite", get_site(site_id))
    text = (FIXTURE_ROOT / f"shopify_{site_id}" / "collections.json").read_text("utf-8")
    published = {entry["handle"] for entry in json.loads(text)["collections"]}

    assert site.config.collections, f"{site_id} walks the unfiltered catalogue"
    assert set(site.config.collections) <= published
