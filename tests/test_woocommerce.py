from __future__ import annotations

import json
import tomllib
import urllib.robotparser
from typing import TYPE_CHECKING, cast

import pytest

from coffee_aggregator import platforms, sites
from coffee_aggregator.http import FetchDisallowed, FetchResult
from coffee_aggregator.models import ProcessMethod, RoastLevel, RoastProfile
from coffee_aggregator.platforms.shoptet import KNOWN_FIELDS
from coffee_aggregator.platforms.woocommerce import (
    DEFAULT_LABEL_MAP,
    WooConfigError,
    WooSite,
    minor_amount,
)
from coffee_aggregator.sites import CONFIG_DIR
from coffee_aggregator.sites import get as get_site
from coffee_aggregator.sites.base import ProductRef
from conftest import FIXTURE_ROOT

if TYPE_CHECKING:
    from pathlib import Path

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee

KAVALOKA = "woo_kavaloka"
RIPIT = "woo_ripit"
EBENICA = "woo_ebenica"
EBENICA_CATEGORY = "https://ebenica.sk/zrnkova-kava/"
KAVALOKA_PAGE1 = (
    "https://www.kavaloka.cz/wp-json/wc/store/v1/products?per_page=100&page=1&category=19"
)

MINIMAL_TOML = """
platform = "woocommerce"
site_id = "tinyroastery"
name = "Tiny Roastery"
country = "SK"
base_url = "https://tiny.sk"
currency = "EUR"
api_category_ids = [7]
"""


class FakeFetcher:
    """Serves canned API pages and listings without any network."""

    def __init__(self, bodies: dict[str, str], disallowed: tuple[str, ...] = ()) -> None:
        self.bodies = bodies
        self.disallowed = disallowed
        self.requested: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.requested.append(url)
        if any(marker in url for marker in self.disallowed):
            raise FetchDisallowed(url)
        return FetchResult(
            url, url, 200, self.bodies.get(url, "[]"), from_cache=False, elapsed_s=0.0
        )


def as_fetcher(fake: FakeFetcher) -> PoliteFetcher:
    return cast("PoliteFetcher", fake)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "shop.toml"
    path.write_text(body, "utf-8")
    return path


def _items(site_id: str) -> list[dict[str, object]]:
    text = (FIXTURE_ROOT / f"woo_{site_id}" / "store_products_page1.json").read_text("utf-8")
    return cast("list[dict[str, object]]", json.loads(text))


def _by_id(site_id: str, product_id: int) -> dict[str, object]:
    return next(item for item in _items(site_id) if item["id"] == product_id)


def parse_api(site: WooSite, item: dict[str, object]) -> Coffee:
    ref = site._api_ref(item)
    assert ref is not None
    coffee = site.parse_product("", ref)
    assert coffee is not None
    return coffee


@pytest.fixture
def kavaloka() -> WooSite:
    return cast("WooSite", get_site("kavaloka"))


@pytest.fixture
def ripit() -> WooSite:
    return cast("WooSite", get_site("ripit"))


@pytest.fixture
def ebenica() -> WooSite:
    return cast("WooSite", get_site("ebenica"))


# --- the three configured shops are registered without any Python change -----


def test_the_shipped_shops_are_registered_from_toml_alone(
    kavaloka: WooSite,
    ripit: WooSite,
    ebenica: WooSite,
) -> None:
    assert (kavaloka.kind, kavaloka.country, kavaloka.config.mode) == ("woocommerce", "CZ", "api")
    assert (ripit.kind, ripit.country, ripit.config.mode) == ("woocommerce", "SK", "api")
    assert (ebenica.kind, ebenica.country, ebenica.config.mode) == ("woocommerce", "SK", "html")
    assert kavaloka.config.api_category_ids == [19, 20, 21]
    assert ripit.config.api_category_ids == [16]
    assert ebenica.config.category_urls == [EBENICA_CATEGORY]


# --- configuration validation ------------------------------------------------


def test_an_unknown_label_map_field_is_rejected(tmp_path: Path) -> None:
    body = MINIMAL_TOML + '\n[label_map]\n"Bližšie určenie" = "regoin"\n'
    with pytest.raises(WooConfigError) as excinfo:
        platforms.build_from_config(_write(tmp_path, body))
    message = str(excinfo.value)
    assert "Bližšie určenie" in message
    assert "regoin" in message
    assert "region" in message  # the valid fields are listed


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (
            'platform = "woocommerce"\nname = "x"\ncountry = "SK"\nbase_url = "https://x/"\n',
            "site_id",
        ),
        (MINIMAL_TOML.replace('country = "SK"', 'country = "PL"'), "country"),
        (MINIMAL_TOML + 'mode = "graphql"\n', "mode"),
        (MINIMAL_TOML + 'mode = "html"\n', "category_urls"),
        (MINIMAL_TOML.replace("api_category_ids = [7]", 'api_category_ids = ["kava"]'), "integers"),
    ],
)
def test_a_broken_toml_is_rejected_with_an_actionable_message(
    tmp_path: Path,
    body: str,
    problem: str,
) -> None:
    with pytest.raises(WooConfigError) as excinfo:
        platforms.build_from_config(_write(tmp_path, body))
    assert problem in str(excinfo.value)


def test_every_default_label_points_at_a_known_field() -> None:
    assert set(DEFAULT_LABEL_MAP.values()) <= KNOWN_FIELDS


# --- discovery: pagination stops on an empty page ----------------------------


def _page(ids: list[int]) -> str:
    return json.dumps(
        [
            {
                "id": identifier,
                "name": f"Káva {identifier}",
                "permalink": f"https://tiny.sk/produkt/{identifier}/",
                "prices": {"price": "1250", "currency_code": "EUR", "currency_minor_unit": 2},
            }
            for identifier in ids
        ]
    )


def _url(page: int) -> str:
    return f"https://tiny.sk/wp-json/wc/store/v1/products?per_page=100&page={page}&category=7"


def test_api_discovery_stops_on_the_first_empty_page(tmp_path: Path) -> None:
    adapter = cast("WooSite", platforms.build_from_config(_write(tmp_path, MINIMAL_TOML)))
    fake = FakeFetcher({_url(1): _page([1, 2]), _url(2): _page([3]), _url(3): "[]"})

    refs = list(adapter.discover(as_fetcher(fake)))

    assert [ref.external_id for ref in refs] == ["1", "2", "3"]
    assert fake.requested == [_url(1), _url(2), _url(3)]


def test_api_discovery_never_trusts_a_total_header(tmp_path: Path) -> None:
    """kavavitazov.sk reports X-WP-Total: 1 while serving more, so only an
    empty page ends the walk — and FetchResult exposes no headers anyway."""
    adapter = cast("WooSite", platforms.build_from_config(_write(tmp_path, MINIMAL_TOML)))
    fake = FakeFetcher({_url(1): _page([1]), _url(2): _page([2]), _url(3): "[]"})

    assert [ref.external_id for ref in adapter.discover(as_fetcher(fake))] == ["1", "2"]


def test_api_discovery_respects_max_pages(tmp_path: Path) -> None:
    adapter = cast("WooSite", platforms.build_from_config(_write(tmp_path, MINIMAL_TOML)))
    fake = FakeFetcher({_url(1): _page([1]), _url(2): _page([2]), _url(3): _page([3])})

    refs = list(adapter.discover(as_fetcher(fake), max_pages=2))

    assert [ref.external_id for ref in refs] == ["1", "2"]
    assert fake.requested == [_url(1), _url(2)]


def test_discovery_carries_the_payload_so_no_detail_page_is_fetched(tmp_path: Path) -> None:
    adapter = cast("WooSite", platforms.build_from_config(_write(tmp_path, MINIMAL_TOML)))
    fake = FakeFetcher({_url(1): _page([1]), _url(2): "[]"})

    ref = next(iter(adapter.discover(as_fetcher(fake))))

    assert ref.payload is not None
    assert json.loads(ref.payload)["id"] == 1
    assert ref.url == "https://tiny.sk/produkt/1/"
    assert all("/produkt/" not in url for url in fake.requested)


def test_the_ignore_list_drops_a_product_before_it_costs_anything(tmp_path: Path) -> None:
    body = MINIMAL_TOML + '\nignore = ["darcek"]\n'
    adapter = cast("WooSite", platforms.build_from_config(_write(tmp_path, body)))
    page = json.dumps(
        [
            {
                "id": 1,
                "name": "Darčeková poukážka",
                "permalink": "https://tiny.sk/p/1/",
                "prices": {},
            },
            {"id": 2, "name": "Kolumbia Huila", "permalink": "https://tiny.sk/p/2/", "prices": {}},
            {"id": 3, "name": "Cascara čaj", "permalink": "https://tiny.sk/p/3/", "prices": {}},
        ]
    )
    fake = FakeFetcher({_url(1): page, _url(2): "[]"})

    assert [ref.external_id for ref in adapter.discover(as_fetcher(fake))] == ["2"]


def test_slugs_are_resolved_against_the_categories_endpoint(tmp_path: Path) -> None:
    """Synthetic 16-digit ids (caffe4u.sk) are never preferred over real ones."""
    body = MINIMAL_TOML.replace("api_category_ids = [7]", 'api_category_slugs = ["kava"]')
    adapter = cast("WooSite", platforms.build_from_config(_write(tmp_path, body)))
    categories = "https://tiny.sk/wp-json/wc/store/v1/products/categories?per_page=100"
    products = "https://tiny.sk/wp-json/wc/store/v1/products?per_page=100&page=1&category=31"
    fake = FakeFetcher(
        {
            categories: json.dumps(
                [
                    {"id": 9003241321018644, "slug": "kava", "name": "Káva"},
                    {"id": 31, "slug": "kava", "name": "Káva"},
                    {"id": 44, "slug": "caj", "name": "Čaj"},
                ]
            ),
            products: _page([5]),
        }
    )

    refs = list(adapter.discover(as_fetcher(fake)))

    assert [ref.external_id for ref in refs] == ["5"]
    assert fake.requested[0] == categories


# --- prices: minor units are not always hundredths ---------------------------


@pytest.mark.parametrize(
    ("raw", "minor_unit", "expected"),
    [
        ("28700", 2, 287.0),  # a Czech shop quoting hundredths
        ("1800", 2, 18.0),  # a Slovak shop quoting cents
        ("360", 0, 360.0),  # kmen.coffee / kavaloka.cz quote whole crowns
        ("83", 0, 83.0),
        ("0", 2, None),  # "price on request", not a free bag of coffee
        ("", 2, None),
        (None, 2, None),
    ],
)
def test_minor_unit_conversion(raw: str | None, minor_unit: int, expected: float | None) -> None:
    assert minor_amount(raw, minor_unit) == expected


def test_a_whole_crown_shop_is_not_priced_a_hundredfold_low(kavaloka: WooSite) -> None:
    coffee = parse_api(kavaloka, _by_id("kavaloka", 9059))

    assert coffee.name == "Rwanda Isano"
    assert (coffee.price, coffee.currency) == (83.0, "CZK")
    assert coffee.weight_g == 100  # prices.price is the cheapest variant
    assert coffee.price_per_kg == 830.0


def test_a_euro_shop_converts_cents(ripit: WooSite) -> None:
    coffee = parse_api(ripit, _by_id("ripit", 3895))

    assert coffee.name == "Tofišot – El Salvador"
    assert (coffee.price, coffee.currency, coffee.weight_g) == (9.5, "EUR", 250)
    assert coffee.price_per_kg == 38.0


def test_a_sale_price_records_the_price_before_the_discount(ripit: WooSite) -> None:
    item = dict(_by_id("ripit", 3895))
    item["prices"] = {
        **cast("dict[str, object]", item["prices"]),
        "price": "800",
        "regular_price": "950",
    }

    coffee = parse_api(ripit, item)

    assert (coffee.price, coffee.original_price) == (8.0, 9.5)


# --- attributes become canonical values --------------------------------------


def test_attributes_map_onto_canonical_fields(ripit: WooSite) -> None:
    coffee = parse_api(ripit, _by_id("ripit", 4222))

    assert coffee.name == "Samršot – Colombia"
    assert coffee.origin.country == "CO"  # "Pôvod: Colombia" -> ISO 3166
    assert coffee.origin.region == "Quindio"
    assert (coffee.origin.altitude_min_m, coffee.origin.altitude_max_m) == (1500, 1750)
    assert coffee.origin.variety == ["Caturra", "Pink Bourbon"]
    assert coffee.processing.raw == "Co-fermented"
    assert coffee.processing.method is ProcessMethod.OTHER
    assert coffee.roast.level is RoastLevel.LIGHT
    assert coffee.roast.profile is RoastProfile.FILTER
    assert coffee.taste.flavor_notes == ["červený melón", "med", "liči"]
    assert coffee.taste.brewing_methods == ["Filter"]
    assert (coffee.taste.body, coffee.taste.acidity, coffee.taste.sweetness) == (5, 4, 5)
    assert coffee.species.arabica_pct == 100
    assert coffee.species.is_blend is False
    assert coffee.categories == ["Filter", "Kávy"]
    # "Typ" and "Zdroj" are ignored by the shop's label_map but still stored.
    assert coffee.raw_attributes["TYP"] == "Single origin"


def test_a_czech_shop_maps_its_own_spellings(kavaloka: WooSite) -> None:
    coffee = parse_api(kavaloka, _by_id("kavaloka", 9059))

    assert coffee.origin.country == "RW"  # "Země původu: Rwanda (Lake Kivu)"
    assert (coffee.origin.altitude_min_m, coffee.origin.altitude_max_m) == (1700, 2000)
    assert coffee.origin.variety == ["Red Bourbon"]
    assert coffee.processing.method is ProcessMethod.WASHED
    assert coffee.roast.level is RoastLevel.MEDIUM
    assert coffee.taste.acidity == 3  # "Kyselost: střední" -> the 1-5 scale
    assert coffee.taste.flavor_notes == ["rybíz", "černý čaj", "hořká čokoláda"]
    assert "espresso" in coffee.taste.brewing_methods
    assert coffee.species.arabica_pct == 100
    assert coffee.raw_attributes["SKU"] == "1126-1-1"


def test_an_unmapped_attribute_still_reaches_raw_attributes(kavaloka: WooSite) -> None:
    coffee = parse_api(kavaloka, _by_id("kavaloka", 9059))

    assert coffee.raw_attributes["NADMOŘSKÁ VÝŠKA"] == "1700 - 2000 m n. m."
    assert coffee.raw_attributes["VELIKOST BALENÍ"].startswith("100 g")


# --- descriptions: Label: value lines and tables ------------------------------


def test_label_lines_in_the_description_feed_the_same_fields(tmp_path: Path) -> None:
    """16 surveyed shops state origin only as ``Label: value`` prose lines."""
    adapter = cast("WooSite", platforms.build_from_config(_write(tmp_path, MINIMAL_TOML)))
    item = {
        "id": 11,
        "name": "Etiópia Guji",
        "permalink": "https://tiny.sk/produkt/guji/",
        "prices": {"price": "1450", "currency_code": "EUR", "currency_minor_unit": 2},
        "short_description": "<p>Sladká a ovocná.</p>",
        "description": (
            "<p><strong>Odroda:</strong> Heirloom<br/>"
            "Nadmorská výška: 1 950 m n. m.<br/>"
            "Spracovanie: Natural<br/>"
            "Krajina pôvodu: Etiópia</p><p>Pražíme ju na filter.</p>"
        ),
    }

    coffee = parse_api(adapter, item)

    assert coffee.origin.variety == ["Heirloom"]
    assert coffee.origin.altitude_min_m == 1950
    assert coffee.processing.method is ProcessMethod.NATURAL
    assert coffee.origin.country == "ET"
    assert coffee.description is not None
    assert "Pražíme ju na filter." in coffee.description


def test_a_table_inside_the_short_description_is_read_too(tmp_path: Path) -> None:
    """severan.eu ships its facts as <table class="coffee-table">, not as lines."""
    adapter = cast("WooSite", platforms.build_from_config(_write(tmp_path, MINIMAL_TOML)))
    item = {
        "id": 12,
        "name": "Brazília Santos",
        "permalink": "https://tiny.sk/produkt/santos/",
        "prices": {"price": "990", "currency_code": "EUR", "currency_minor_unit": 2},
        "short_description": (
            '<table class="coffee-table">'
            "<tr><td>Spracovanie</td><td>Washed</td></tr>"
            "<tr><td>Praženie</td><td>tmavé</td></tr>"
            "</table>"
        ),
    }

    coffee = parse_api(adapter, item)

    assert coffee.processing.method is ProcessMethod.WASHED
    assert coffee.roast.level is RoastLevel.DARK


# --- variations become variants ----------------------------------------------


def test_variations_become_variants_with_weights_from_the_term_labels(
    kavaloka: WooSite,
) -> None:
    coffee = parse_api(kavaloka, _by_id("kavaloka", 9059))

    assert len(coffee.variants) == 24
    first = coffee.variants[0]
    assert (first.weight_g, first.label) == (100, "100 g / zrnková")
    assert {variant.weight_g for variant in coffee.variants} == {100, 250, 500, 1000}
    # The Store API states only the cheapest price for a variable product, so a
    # per-variant price would be a guess.
    assert all(variant.price is None for variant in coffee.variants)
    assert all(variant.external_id for variant in coffee.variants)


def test_a_single_variation_keeps_the_headline_price(ripit: WooSite) -> None:
    coffee = parse_api(ripit, _by_id("ripit", 4222))

    assert [(v.weight_g, v.price, v.currency) for v in coffee.variants] == [(250, 18.0, "EUR")]


def test_a_product_without_a_price_range_still_parses(ripit: WooSite) -> None:
    """doraz.sk, ripit.sk and triproasters.sk run a build that omits it."""
    item = _by_id("ripit", 4222)
    assert cast("dict[str, object]", item["prices"])["price_range"] is None

    coffee = parse_api(ripit, item)

    assert coffee.price == 18.0


# --- HTML mode ----------------------------------------------------------------


def test_html_listing_reads_every_card(ebenica: WooSite) -> None:
    body = (FIXTURE_ROOT / EBENICA / "list_page1.html").read_text("utf-8")

    refs = ebenica.parse_listing(body)

    assert len(refs) == 12
    first = refs[0]
    assert (first.external_id, first.name) == ("176593", "BLEND XVII")
    assert first.url == "https://ebenica.sk/kava-blendxvii/"
    assert (first.price, first.currency) == (13.9, "EUR")
    assert len({ref.external_id for ref in refs}) == len(refs)


def test_html_pagination_follows_the_localised_next_link(ebenica: WooSite) -> None:
    body = (FIXTURE_ROOT / EBENICA / "list_page1.html").read_text("utf-8")

    assert ebenica.next_page_url(body, EBENICA_CATEGORY, 1) == (
        "https://ebenica.sk/zrnkova-kava/strana/2/"
    )


def test_html_detail_page_yields_canonical_fields(ebenica: WooSite) -> None:
    body = (FIXTURE_ROOT / EBENICA / "detail_colombia_la_secreta.html").read_text("utf-8")
    ref = ProductRef(
        site_id="ebenica",
        external_id="51422",
        url="https://ebenica.sk/kava-colombia-la-secreta/",
    )

    coffee = ebenica.parse_product(body, ref)

    assert coffee is not None
    assert coffee.name == "Colombia La Secreta"
    assert (coffee.price, coffee.currency, coffee.weight_g) == (4.49, "EUR", 70)
    assert coffee.available is True
    assert coffee.origin.country == "CO"
    assert coffee.origin.producer == "Juan Carlos Meija, La Secreta, Cafelumbus"
    assert (coffee.origin.altitude_min_m, coffee.origin.altitude_max_m) == (1700, 2050)
    assert coffee.origin.variety == ["Caturra"]
    assert coffee.processing.method is ProcessMethod.WASHED
    assert coffee.roast.level is RoastLevel.MEDIUM
    assert coffee.taste.sca_score == 84.0
    assert coffee.specialty_grade is True
    assert coffee.species.arabica_pct == 100
    assert coffee.raw_attributes["OCENENIA"].startswith("Great Taste")
    assert coffee.raw_attributes["OG_TITLE"]


def test_html_detail_page_prices_every_weight(ebenica: WooSite) -> None:
    """data-product_variations is the one place a Woo shop states price per weight."""
    body = (FIXTURE_ROOT / EBENICA / "detail_colombia_la_secreta.html").read_text("utf-8")
    ref = ProductRef(site_id="ebenica", external_id="51422", url="https://ebenica.sk/x/")

    coffee = ebenica.parse_product(body, ref)

    assert coffee is not None
    priced = [(v.weight_g, v.price) for v in coffee.variants]
    assert (70, 4.49) in priced
    assert (1000, 39.9) in priced
    assert coffee.variants[-1].available is False


# --- robots.txt gates the API exactly like a page -----------------------------


def test_robots_txt_closes_the_store_api_on_the_html_mode_shop() -> None:
    parser = urllib.robotparser.RobotFileParser()
    parser.parse((FIXTURE_ROOT / EBENICA / "robots.txt").read_text("utf-8").splitlines())

    assert not parser.can_fetch(
        "coffee-aggregator", "https://ebenica.sk/wp-json/wc/store/v1/products"
    )
    assert parser.can_fetch("coffee-aggregator", EBENICA_CATEGORY)


def test_the_open_shops_allow_the_store_api() -> None:
    for site_id, host in (("kavaloka", "https://www.kavaloka.cz"), ("ripit", "https://ripit.sk")):
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(
            (FIXTURE_ROOT / f"woo_{site_id}" / "robots.txt").read_text("utf-8").splitlines()
        )
        assert parser.can_fetch("coffee-aggregator", f"{host}/wp-json/wc/store/v1/products")


# --- automatic fallback -------------------------------------------------------


def test_an_html_answer_to_the_api_falls_back_to_the_html_listing(tmp_path: Path) -> None:
    body = MINIMAL_TOML + '\ncategory_urls = ["/kava/"]\n'
    adapter = cast("WooSite", platforms.build_from_config(_write(tmp_path, body)))
    card = """
    <html><body><ul class="products"><li class="product post-42">
      <a class="woocommerce-LoopProduct-link" href="/produkt/kolumbia/"></a>
      <h2 class="woocommerce-loop-product__title">Kolumbia La Paz</h2>
      <span class="price"><bdi>12,50 €</bdi></span>
    </li></ul></body></html>
    """
    fake = FakeFetcher({_url(1): "<!DOCTYPE html><html>404</html>", "https://tiny.sk/kava/": card})

    refs = list(adapter.discover(as_fetcher(fake)))

    assert [ref.external_id for ref in refs] == ["42"]
    assert refs[0].payload is None  # the HTML path pays for a detail fetch
    assert fake.requested == [_url(1), "https://tiny.sk/kava/"]


def test_a_robots_refusal_of_wp_json_falls_back_to_the_html_listing(tmp_path: Path) -> None:
    body = MINIMAL_TOML + '\ncategory_urls = ["/kava/"]\n'
    adapter = cast("WooSite", platforms.build_from_config(_write(tmp_path, body)))
    card = '<ul class="products"><li class="product post-9"><a href="/p/9/"></a></li></ul>'
    fake = FakeFetcher({"https://tiny.sk/kava/": card}, disallowed=("/wp-json/",))

    refs = list(adapter.discover(as_fetcher(fake)))

    assert [ref.external_id for ref in refs] == ["9"]
    assert fake.requested == [_url(1), "https://tiny.sk/kava/"]


def test_an_html_mode_shop_never_asks_the_store_api(ebenica: WooSite) -> None:
    fake = FakeFetcher(
        {EBENICA_CATEGORY: (FIXTURE_ROOT / EBENICA / "list_page1.html").read_text("utf-8")}
    )

    refs = list(ebenica.discover(as_fetcher(fake), max_pages=1))

    assert len(refs) == 12
    assert all("wp-json" not in url for url in fake.requested)


# --- every shipped woo config parses its own saved pages ---------------------


def _woo_config_ids() -> list[str]:
    found: list[str] = []
    for path in sorted(CONFIG_DIR.glob("*.toml")):
        with path.open("rb") as handle:
            config = tomllib.load(handle)
        if str(config.get("platform", "")).lower() == "woocommerce":
            found.append(str(config["site_id"]))
    return found


SHIPPED = _woo_config_ids()


def test_the_shipped_woo_configs_are_the_ones_under_test() -> None:
    assert SHIPPED == ["ebenica", "kavaloka", "ripit"]


@pytest.mark.parametrize("site_id", SHIPPED)
def test_every_shipped_config_is_registered(site_id: str) -> None:
    registered = {adapter.site_id for adapter in sites.all_sites()}
    assert site_id in registered


@pytest.mark.parametrize("site_id", SHIPPED)
def test_every_shipped_config_reads_its_saved_pages(site_id: str) -> None:
    site = cast("WooSite", get_site(site_id))
    directory = FIXTURE_ROOT / f"woo_{site_id}"
    assert directory.is_dir(), f"{site_id} ships no fixtures"

    coffees = [
        parse_api(site, item) for item in (_items(site_id) if site.config.mode == "api" else [])[:3]
    ]
    for path in sorted(directory.glob("detail_*.html")):
        ref = ProductRef(site_id=site_id, external_id="1", url=f"{site.base_url}p/")
        coffee = site.parse_product(path.read_text("utf-8"), ref)
        assert coffee is not None, path
        coffees.append(coffee)

    assert coffees, f"{site_id} parsed nothing"
    for coffee in coffees:
        assert coffee.name
        assert coffee.price is not None
        assert coffee.currency in {"CZK", "EUR"}
        assert coffee.weight_g is not None
        assert coffee.price_per_kg is not None
        assert coffee.origin.country is None or len(coffee.origin.country) == 2
        assert coffee.processing.method in set(ProcessMethod)
        assert coffee.roast.level in set(RoastLevel)
        assert coffee.roast.profile in set(RoastProfile)
        assert len(coffee.raw_attributes) >= 4, coffee.raw_attributes


@pytest.mark.parametrize("site_id", SHIPPED)
def test_every_shipped_config_lists_a_reachable_fallback(site_id: str) -> None:
    site = cast("WooSite", get_site(site_id))
    assert site.config.category_urls, f"{site_id} has no HTML fallback"
    assert all(url.startswith(site.base_url) for url in site.config.category_urls)
