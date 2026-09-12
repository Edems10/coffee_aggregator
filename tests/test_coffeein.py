from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, cast

import pytest

from coffee_aggregator.http import FetchResult
from coffee_aggregator.models import ProcessMethod, RoastLevel, RoastProfile
from coffee_aggregator.pipeline import run
from coffee_aggregator.sinks.base import SinkResult
from coffee_aggregator.sites import get as get_site
from coffee_aggregator.sites.base import ProductRef, SiteAdapter
from coffee_aggregator.sites.coffeein import CoffeeinSite

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee

BASE = "https://www.coffeein.sk"
LIST_URL = f"{BASE}/kategoria/2/cerstvo-prazena-zrnkova-kava/1/"


class FakeFetcher:
    """Serves canned listing and detail pages without any network."""

    def __init__(self, bodies: dict[str, str]) -> None:
        self.bodies = bodies
        self.requested: list[str] = []
        self.redirect_to: str | None = None

    def get(self, url: str) -> FetchResult:
        self.requested.append(url)
        body = self.bodies.get(url, "<html><body></body></html>")
        final = self.redirect_to if self.redirect_to and url not in self.bodies else url
        return FetchResult(url, final or url, 200, body, from_cache=False, elapsed_s=0.0)

    def fetch_many(self, urls: Sequence[str]) -> list[FetchResult]:
        return [self.get(url) for url in urls]


class CollectingSink:
    """Keeps every coffee the pipeline writes."""

    def __init__(self) -> None:
        self.written: list[Coffee] = []

    def upsert(self, coffees: Sequence[Coffee]) -> SinkResult:
        self.written.extend(coffees)
        return SinkResult(written=len(coffees), failed=0)

    def mark_delisted(self, site_id: str, seen_external_ids: set[str]) -> int:
        return 0

    def close(self) -> None:
        return None


def fetcher_for(bodies: dict[str, str]) -> tuple[CoffeeinSite, FakeFetcher, PoliteFetcher]:
    site = CoffeeinSite()
    fake = FakeFetcher(bodies)
    return site, fake, cast("PoliteFetcher", fake)


@pytest.fixture
def site() -> CoffeeinSite:
    return CoffeeinSite()


@pytest.fixture
def refs(site: CoffeeinSite, fixture_html: Callable[[str, str], str]) -> list[ProductRef]:
    return site.parse_listing(fixture_html("coffeein", "list_page1.html"))


def parse(
    site: CoffeeinSite,
    fixture_html: Callable[[str, str], str],
    name: str,
    external_id: str,
) -> Coffee:
    ref = ProductRef(
        site_id="coffeein",
        external_id=external_id,
        url=f"{BASE}/detail/{external_id}/x/",
    )
    coffee = site.parse_product(fixture_html("coffeein", name), ref)
    assert coffee is not None
    return coffee


@pytest.fixture
def cuba(site: CoffeeinSite, fixture_html: Callable[[str, str], str]) -> Coffee:
    return parse(site, fixture_html, "detail_151_cuba.html", "151")


@pytest.fixture
def elite(site: CoffeeinSite, fixture_html: Callable[[str, str], str]) -> Coffee:
    return parse(site, fixture_html, "detail_115_elite_blend.html", "115")


@pytest.fixture
def vietnam(site: CoffeeinSite, fixture_html: Callable[[str, str], str]) -> Coffee:
    return parse(site, fixture_html, "detail_668_vietnam.html", "668")


# --------------------------------------------------------------------------- registry


def test_the_adapter_registers_itself() -> None:
    adapter = get_site("coffeein")
    assert isinstance(adapter, CoffeeinSite)
    assert adapter.base_url == "https://www.coffeein.sk/"
    assert adapter.country == "SK"


# --------------------------------------------------------------------------- discovery


def test_listing_yields_twelve_refs_with_real_urls(refs: list[ProductRef]) -> None:
    assert len(refs) == 12
    ids = [ref.external_id for ref in refs]
    assert {"151", "115", "668"} <= set(ids)
    assert len(set(ids)) == len(ids)
    by_id = {ref.external_id: ref for ref in refs}
    assert by_id["151"].url == f"{BASE}/detail/151/cuba-sierra-maestra-200-g-zrnkova-kava/"
    assert all(ref.url.startswith(f"{BASE}/detail/") for ref in refs)


def test_listing_keeps_diacritics_and_carries_card_data(refs: list[ProductRef]) -> None:
    by_id = {ref.external_id: ref for ref in refs}
    vietnam = by_id["668"]
    assert vietnam.name == "Vietnam Lang Biang NATURAL - stredné praženie (200 g, zrnková káva)"
    assert vietnam.price == 12.5
    assert vietnam.currency == "EUR"
    assert vietnam.image_url is not None
    assert vietnam.image_url.startswith(f"{BASE}/images/products/")
    assert vietnam.extra["flavor_notes"] == "sušené ovocie, trstinový cukor, mandarínky"
    assert vietnam.extra["stock"] == "Skladom"
    assert "top" in vietnam.extra["tags"]


def test_quotes_in_a_name_do_not_break_discovery(site: CoffeeinSite) -> None:
    html = """
    <ul class="product_list">
      <li class="product">
        <div class="prod_img" style="background-image:url(/images/products/small_9_x.jpg);"></div>
        <h2><a class="headline" href="/detail/9/tom-s-o-brien-quot-special-quot-200-g/">
          Tom's O'Brien "Special" (200 g, zrnková káva)</a></h2>
        <form onsubmit="return add_to_cart('9', 'a', {'added_item_price':'8.50',
          'added_item_name':'Tom's O'Brien'}, event);"><span>8,50 &euro;</span></form>
      </li>
    </ul>
    """
    refs = site.parse_listing(html)
    assert len(refs) == 1
    assert refs[0].name == "Tom's O'Brien \"Special\" (200 g, zrnková káva)"
    assert refs[0].url == f"{BASE}/detail/9/tom-s-o-brien-quot-special-quot-200-g/"
    assert refs[0].price == 8.5


def test_cards_without_a_detail_link_are_skipped(site: CoffeeinSite) -> None:
    html = (
        '<ul class="product_list"><li class="product">'
        '<h2><a class="headline" href="/blog/">x</a></h2></li></ul>'
    )
    assert site.parse_listing(html) == []


def test_discover_walks_pages_until_a_page_repeats(
    fixture_html: Callable[[str, str], str],
) -> None:
    page1 = fixture_html("coffeein", "list_page1.html")
    site, fake, fetcher = fetcher_for({LIST_URL: page1})
    site.max_pages = 3
    refs = list(site.discover(fetcher))
    assert len(refs) == 12
    assert fake.requested[0] == LIST_URL
    assert len(fake.requested) == 2


def test_discover_stops_on_a_redirect(fixture_html: Callable[[str, str], str]) -> None:
    page1 = fixture_html("coffeein", "list_page1.html")
    page2_url = f"{BASE}/kategoria/2/cerstvo-prazena-zrnkova-kava/2/"
    site, fake, fetcher = fetcher_for({LIST_URL: page1})
    fake.redirect_to = LIST_URL
    site.max_pages = 5
    refs = list(site.discover(fetcher))
    assert len(refs) == 12
    assert fake.requested == [LIST_URL, page2_url]


def test_discover_can_use_the_sitemap(fixture_html: Callable[[str, str], str]) -> None:
    sitemap = fixture_html("coffeein", "sitemap.xml")
    site, fake, fetcher = fetcher_for({f"{BASE}/sitemap.xml": sitemap})
    site.use_sitemap = True
    refs = list(site.discover(fetcher))
    assert fake.requested == [f"{BASE}/sitemap.xml"]
    assert len(refs) > 100
    assert all(ref.external_id.isdigit() for ref in refs)
    assert len({ref.external_id for ref in refs}) == len(refs)


# --------------------------------------------------------------------------- detail pages


def test_cuba_single_origin(cuba: Coffee) -> None:
    assert cuba.name == "Cuba Sierra Maestra (200 g, zrnková káva)"
    assert cuba.external_id == "151"
    assert (cuba.price, cuba.currency, cuba.weight_g) == (9.99, "EUR", 200)
    assert cuba.price_per_kg == 49.95
    assert cuba.available is True
    assert cuba.decaf is False
    assert cuba.origin.country == "CU"
    assert cuba.origin.region == "Oriente - Sierra Maestra"
    assert cuba.origin.farm == "drobní farmári z regiónu"
    assert cuba.origin.washing_station == "Sierra Maestra"
    assert cuba.origin.variety == ["Typica"]
    assert (cuba.origin.altitude_min_m, cuba.origin.altitude_max_m) == (1200, 1600)
    assert cuba.processing.method is ProcessMethod.WASHED
    assert cuba.processing.raw == "mokré, sušené na slnku"
    assert cuba.species.arabica_pct == 100
    assert cuba.species.is_blend is False


def test_cuba_roast_popularity_and_media(cuba: Coffee) -> None:
    assert cuba.roast.roast_date == date(2026, 9, 11)
    assert cuba.roast.best_before == date(2027, 9, 11)
    assert cuba.roast.level is RoastLevel.MEDIUM_DARK
    assert cuba.roast.raw == "Full City"
    assert cuba.roast.profile is RoastProfile.ESPRESSO
    assert cuba.popularity.rating == 5.0
    assert cuba.popularity.rating_max == 5
    assert cuba.popularity.review_count == 51
    assert cuba.popularity.sold_count is not None
    assert cuba.popularity.sold_count > 0
    first = cuba.popularity.reviews[0]
    assert first.author == "Michal Kulich"
    assert first.date == date(2026, 8, 16)
    assert first.rating == 5.0
    assert first.text is not None
    assert cuba.awards
    assert len(cuba.images) == 4
    assert all(image.startswith(f"{BASE}/images/products/") for image in cuba.images)
    assert cuba.tags == ["Najpredávanejšie"]
    assert "Čerstvo pražená zrnková káva" in cuba.categories


def test_cuba_taste_bars_and_notes(cuba: Coffee) -> None:
    assert (cuba.taste.body, cuba.taste.bitterness) == (3, 4)
    assert (cuba.taste.acidity, cuba.taste.sweetness) == (1, 4)
    assert cuba.taste.scale_max == 5
    assert cuba.taste.flavor_notes == ["kakao", "karamel", "tabakové listy"]
    assert cuba.taste.tasting_text is not None
    assert "dymové" in cuba.taste.tasting_text
    assert cuba.origin_text is not None
    assert "Sierra Maestra" in cuba.origin_text
    assert cuba.description is not None
    assert "Objavte skutočný poklad" in cuba.description


def test_cuba_variants_link_the_one_kilo_sibling(cuba: Coffee) -> None:
    by_id = {variant.external_id: variant for variant in cuba.variants}
    assert set(by_id) == {"151", "520"}
    kilo = by_id["520"]
    assert kilo.weight_g == 1000
    assert kilo.url == f"{BASE}/detail/520/cuba-sierra-maestra-1000-g-zrnkova-kava/"
    assert by_id["151"].weight_g == 200
    assert by_id["151"].price == 9.99


def test_elite_blend_is_kept_with_its_split(elite: Coffee) -> None:
    assert elite.species.arabica_pct == 90
    assert elite.species.robusta_pct == 10
    assert elite.species.is_blend is True
    assert elite.origin.country is None  # a blend's origin prose names its components
    assert elite.weight_g == 200
    assert "508" in {variant.external_id for variant in elite.variants}


def test_vietnam_single_origin_parses_every_label(vietnam: Coffee) -> None:
    assert vietnam.species.arabica_pct == 100
    assert vietnam.species.robusta_pct == 0
    assert vietnam.species.is_blend is False
    assert vietnam.processing.method is ProcessMethod.NATURAL
    assert (vietnam.origin.altitude_min_m, vietnam.origin.altitude_max_m) == (1500, 1700)
    assert vietnam.origin.altitude_raw == "1500 - 1700 m n. m."
    assert vietnam.origin.harvest is not None
    assert "2026" in vietnam.origin.harvest
    assert vietnam.origin.country == "VN"
    assert vietnam.origin.region == "Dalat, Lang Biang"
    assert vietnam.origin.washing_station == "ZanYa"
    assert vietnam.origin.variety == ["Bourbon", "Typica", "Caturra", "Catimor"]
    assert len(vietnam.taste.brewing_methods) == 3
    assert vietnam.roast.level is RoastLevel.MEDIUM
    assert vietnam.specialty_grade is True
    assert vietnam.variants == []


def test_vietnam_keeps_every_label_in_raw_attributes(vietnam: Coffee) -> None:
    raw = vietnam.raw_attributes
    assert len(raw) >= 6
    assert raw["REGIÓN"] == "Dalat, Lang Biang"
    assert raw["SPRACOVATEĽ"] == "Marián Takáč"
    assert raw["ZBER"] == "ručný selektívny, 2026"
    assert raw["ODTIEŇ PRAŽENIA"] == "City +"
    assert raw["VEĽKOSŤ BALENIA"] == "200 g (zrnková káva)"
    assert all(key == key.upper() == key.strip() for key in raw)


def test_the_external_id_falls_back_to_the_gtag_item_id(
    site: CoffeeinSite,
    fixture_html: Callable[[str, str], str],
) -> None:
    ref = ProductRef(site_id="coffeein", external_id="", url=f"{BASE}/detail/151/x/")
    coffee = site.parse_product(fixture_html("coffeein", "detail_151_cuba.html"), ref)
    assert coffee is not None
    assert coffee.external_id == "151"


# --------------------------------------------------------------------------- edge cases


def test_a_page_without_a_description_block_still_parses(site: CoffeeinSite) -> None:
    html = """
    <div class="detail"><div class="popis"><h1 itemprop="name">Holá káva (250 g)</h1></div>
    <div class="cena"><span class="product_price" itemprop="price" content="11.00">11,00
    <span itemprop="priceCurrency" content="EUR">&euro;</span></span></div></div>
    """
    coffee = site.parse_product(html, ProductRef("coffeein", "7", f"{BASE}/detail/7/hola/"))
    assert coffee is not None
    assert coffee.name == "Holá káva (250 g)"
    assert coffee.price == 11.0
    assert coffee.weight_g == 250
    assert coffee.raw_attributes == {}
    assert coffee.description is None
    assert coffee.species.arabica_pct is None
    assert coffee.available is None


def test_zero_percent_is_a_value_not_a_missing_field(site: CoffeeinSite) -> None:
    html = """
    <div class="detail"><h1 itemprop="name">India Kaapi Royal (200 g)</h1>
    <p itemprop="description">Robusta k espressu.<br/><strong>0 % Arabika / 100 % Robusta<br/>
    Odtieň praženia: Full City</strong></p></div>
    """
    coffee = site.parse_product(html, ProductRef("coffeein", "162", f"{BASE}/detail/162/x/"))
    assert coffee is not None
    assert coffee.species.arabica_pct == 0
    assert coffee.species.robusta_pct == 100
    assert coffee.species.is_blend is False


def test_a_multi_line_block_never_glues_the_harvest_year_to_the_species(
    site: CoffeeinSite,
) -> None:
    html = """
    <div class="detail"><h1 itemprop="name">Test (200 g)</h1>
    <p itemprop="description">ZBER: ručný selektívny, 2026<br/>100 % Arabika<br/>
    SPRACOVANIE: natural</p></div>
    """
    coffee = site.parse_product(html, ProductRef("coffeein", "1", f"{BASE}/detail/1/x/"))
    assert coffee is not None
    assert coffee.species.arabica_pct == 100
    assert coffee.processing.raw == "natural"
    assert coffee.processing.method is ProcessMethod.NATURAL


def test_weight_is_none_only_when_the_page_never_states_it(site: CoffeeinSite) -> None:
    html = '<div class="detail"><h1 itemprop="name">Káva bez balenia</h1></div>'
    coffee = site.parse_product(html, ProductRef("coffeein", "2", f"{BASE}/detail/2/x/"))
    assert coffee is not None
    assert coffee.weight_g is None

    labelled = """
    <div class="detail"><h1 itemprop="name">Káva bez balenia</h1>
    <p itemprop="description">Veľkosť balenia: 1 kg (zrnková káva)</p></div>
    """
    coffee = site.parse_product(labelled, ProductRef("coffeein", "2", f"{BASE}/detail/2/x/"))
    assert coffee is not None
    assert coffee.weight_g == 1000


def test_sold_out_products_are_marked_unavailable(site: CoffeeinSite) -> None:
    html = """
    <div class="detail"><h1 itemprop="name">Káva (200 g)</h1>
    <meta itemprop="availability" content="https://schema.org/OutOfStock" />
    <div class="popis_date_data"><div class="dost">Dostupnosť: <span>Vypredané</span></div></div>
    </div>
    """
    coffee = site.parse_product(html, ProductRef("coffeein", "3", f"{BASE}/detail/3/x/"))
    assert coffee is not None
    assert coffee.available is False


def test_availability_falls_back_to_the_slovak_line(site: CoffeeinSite) -> None:
    html = """
    <div class="detail"><h1 itemprop="name">Káva (200 g)</h1>
    <div class="popis_date_data"><div class="dost">Dostupnosť: <span>Vypredané</span></div></div>
    </div>
    """
    coffee = site.parse_product(html, ProductRef("coffeein", "4", f"{BASE}/detail/4/x/"))
    assert coffee is not None
    assert coffee.available is False


def test_a_discount_yields_the_original_price(site: CoffeeinSite) -> None:
    html = """
    <div class="detail"><h1 itemprop="name">Káva (200 g)</h1>
    <div class="price_box"><div class="price_fixed_box">
    <span class="product_price" itemprop="price" content="8.00">8,00</span>
    <form action="/kosik/krok1/5/" method="post" onsubmit="return add_to_cart('5', 'main_add_count',
      {'added_item_price':'8.00', 'added_item_discount':'2.00'}, event);"></form>
    </div></div></div>
    """
    coffee = site.parse_product(html, ProductRef("coffeein", "5", f"{BASE}/detail/5/x/"))
    assert coffee is not None
    assert coffee.original_price == 10.0
    assert coffee.raw_attributes["ADDED_ITEM_DISCOUNT"] == "2.00"


def test_decaf_is_detected_from_the_name(site: CoffeeinSite) -> None:
    html = '<div class="detail"><h1 itemprop="name">BEZKOFEÍNOVÁ káva (200 g)</h1></div>'
    coffee = site.parse_product(html, ProductRef("coffeein", "6", f"{BASE}/detail/6/x/"))
    assert coffee is not None
    assert coffee.decaf is True


@pytest.mark.parametrize(
    "name",
    [
        "Cascara - sušené kávovníkové čerešne (75 g)",
        "Vrátená (nechcená) káva - stredné/tmavé praženie 200g",
        "Tasting pack - 4 x 50 g",
    ],
)
def test_non_coffee_products_are_skipped(site: CoffeeinSite, name: str) -> None:
    assert site.is_ignored(name) is True
    html = f'<div class="detail"><h1 itemprop="name">{name}</h1></div>'
    assert site.parse_product(html, ProductRef("coffeein", "8", f"{BASE}/detail/8/x/")) is None


def test_real_coffees_are_not_ignored(site: CoffeeinSite, refs: list[ProductRef]) -> None:
    kept = [ref for ref in refs if not site.is_ignored(ref.name)]
    assert len(kept) == 8  # 12 cards minus 2 cascaras and 2 returned-coffee lots
    assert {"151", "115", "668"} <= {ref.external_id for ref in kept}
    assert site.is_ignored(None) is False


# --------------------------------------------------------------------------- pipeline


def test_listing_only_data_survives_into_raw_attributes(
    fixture_html: Callable[[str, str], str],
) -> None:
    """The flavour icons only the category card shows must not be lost."""
    site, fake, _fetcher = fetcher_for(
        {
            LIST_URL: fixture_html("coffeein", "list_page1.html"),
            f"{BASE}/detail/668/vietnam-lang-biang-natural-stredne-prazenie-200-g-zrnkova-kava/": (
                fixture_html("coffeein", "detail_668_vietnam.html")
            ),
        }
    )
    sink = CollectingSink()
    report = run(site, cast("PoliteFetcher", fake), sink, max_pages=1)

    assert report.parsed >= 1
    vietnam = next(coffee for coffee in sink.written if coffee.external_id == "668")
    assert vietnam.raw_attributes["LIST_FLAVOR_NOTES"] == (
        "sušené ovocie, trstinový cukor, mandarínky"
    )
    assert vietnam.raw_attributes["LIST_STOCK"] == "Skladom"
    # the detail page's own labels are untouched by the merge
    assert vietnam.raw_attributes["REGIÓN"] == "Dalat, Lang Biang"


# --------------------------------------------------------------------------- shared helpers


def test_page_metadata_reaches_raw_attributes(vietnam: Coffee) -> None:
    """Both adapters keep Open Graph and <meta> through the same helper."""
    assert "META_KEYWORDS" in vietnam.raw_attributes
    assert "Vietnam" in vietnam.raw_attributes["META_KEYWORDS"]
    assert vietnam.raw_attributes["META_DESCRIPTION"]


def test_the_variety_list_drops_the_species_prefix(site: CoffeeinSite) -> None:
    html = """
    <div class="detail"><h1 itemprop="name">Honduras Lempira (250 g)</h1>
    <p itemprop="description">ODRODA: Arabica – Lempira</p></div>
    """
    coffee = site.parse_product(html, ProductRef("coffeein", "9", f"{BASE}/detail/9/x/"))
    assert coffee is not None
    assert coffee.origin.variety == ["Lempira"]


def test_a_lot_naming_two_processes_keeps_both(site: CoffeeinSite) -> None:
    html = """
    <div class="detail"><h1 itemprop="name">Blend (250 g)</h1>
    <p itemprop="description">SPRACOVANIE: Washed · Natural</p></div>
    """
    coffee = site.parse_product(html, ProductRef("coffeein", "10", f"{BASE}/detail/10/x/"))
    assert coffee is not None
    assert coffee.processing.method is ProcessMethod.MIXED
    assert coffee.processing.methods == [ProcessMethod.WASHED, ProcessMethod.NATURAL]


def test_is_ignored_folds_diacritics_through_the_base_class(site: CoffeeinSite) -> None:
    """The adapter no longer overrides it; SiteAdapter folds for every shop."""
    assert CoffeeinSite.is_ignored is SiteAdapter.is_ignored
    assert site.is_ignored("Vrátená (nechcená) káva 200g") is True
