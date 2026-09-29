from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

import pytest

from coffee_aggregator import normalize
from coffee_aggregator.labels import KNOWN_FIELDS, map_label
from coffee_aggregator.models import ProcessMethod, RoastLevel, RoastProfile
from coffee_aggregator.platforms.shoptet import DEFAULT_LABEL_MAP, ShoptetSite
from coffee_aggregator.sites import CONFIG_DIR
from coffee_aggregator.sites import get as get_site
from coffee_aggregator.sites.base import ProductRef
from conftest import FIXTURE_ROOT

if TYPE_CHECKING:
    from coffee_aggregator.models import Coffee

# One shop per template Shoptet shops actually ship, chosen because each reads a
# different part of the page: the standard template is covered by test_shoptet.
PAGES = {
    "melodyroastery": "detail_kolumbia_la_esmeralda.html",
    "kavicka": "detail_brazil_sul_de_minas.html",
    "alacoffee": "detail_ethiopia_negele.html",
    "alegrecafe": "detail_kibingo_honey.html",
    "cafegape": "detail_peru_andes.html",
    "birdsong": "detail_bolivie_teoponte.html",
    "kafista": "detail_kafista_kenya_excellence_zrnkova_kava_100_arabic.html",
    "usteckakava": "detail_burundi_bukeye.html",
    "kofi": "detail_floral_mist_colombia.html",
}


def read(site_id: str) -> Coffee:
    """Parse the saved page of one shop.

    Args:
        site_id: The shop's registered id.

    Returns:
        The parsed coffee.
    """
    site = get_site(site_id)
    assert isinstance(site, ShoptetSite)
    html = (FIXTURE_ROOT / f"shoptet_{site_id}" / PAGES[site_id]).read_text("utf-8")
    ref = ProductRef(site_id=site_id, external_id="1", url=f"{site.base_url}p/")
    coffee = site.parse_product(html, ref)
    assert coffee is not None
    return coffee


@pytest.mark.parametrize("site_id", sorted(PAGES))
def test_every_template_yields_a_priced_coffee(site_id: str) -> None:
    """Whatever the template, the fields a price comparison needs come through."""
    coffee = read(site_id)

    assert coffee.name
    assert coffee.price is not None
    assert coffee.currency in {"CZK", "EUR"}
    assert coffee.weight_g is not None
    assert coffee.price_per_kg is not None
    assert len(coffee.raw_attributes) >= 4


def test_short_description_passport_is_read() -> None:
    """melodyroastery states the lot in .p-short-description, not .basic-description."""
    coffee = read("melodyroastery")

    assert coffee.origin.country == "CO"
    assert coffee.origin.region == "Quindio"
    assert coffee.origin.farm == "La Esmeralda"
    assert coffee.origin.altitude_min_m == 1800
    assert coffee.origin.variety == ["Castillo"]
    assert coffee.processing.method is ProcessMethod.NATURAL
    assert coffee.taste.sca_score == 87.5


def test_a_combined_price_id_select_becomes_variants() -> None:
    """melodyroastery sells every size through one select, priced in the option text."""
    coffee = read("melodyroastery")

    sizes = {(variant.weight_g, variant.price) for variant in coffee.variants}

    assert (250, 18.0) in sizes
    assert (100, 10.0) in sizes
    assert (500, 34.0) in sizes


def test_template_04_is_read_at_all() -> None:
    """kavicka renders p-detail-inner and a class-less parameter table."""
    coffee = read("kavicka")

    assert coffee.origin.country == "BR"
    assert coffee.origin.variety == ["Catuai"]
    assert coffee.origin.altitude_min_m == 800
    assert coffee.origin.altitude_max_m == 1350
    assert coffee.processing.method is ProcessMethod.NATURAL


def test_a_slovak_roast_label_is_understood() -> None:
    """``Stupeň praženia`` is the Slovak of ``Stupeň pražení``; both feed the roast."""
    assert map_label(normalize.fold("Stupeň praženia"), DEFAULT_LABEL_MAP) == "roast"
    assert map_label(normalize.fold("Stupeň pražení"), DEFAULT_LABEL_MAP) == "roast"

    assert read("kavicka").roast.level is RoastLevel.MEDIUM_DARK


def test_a_grid_of_label_value_divs_is_read() -> None:
    """alacoffee writes the lot as <div><strong>Země:</strong> Ethiopia</div>."""
    coffee = read("alacoffee")

    assert coffee.origin.country == "ET"
    assert coffee.origin.region == "Yirgacheffe, Gedeo"
    assert coffee.origin.altitude_min_m == 1995
    assert coffee.origin.altitude_max_m == 2020
    assert coffee.origin.variety == ["Kurume", "Dega", "Wolisho"]
    assert coffee.processing.method is ProcessMethod.ANAEROBIC
    assert coffee.taste.sca_score == 88.0


def test_intensity_words_become_taste_points() -> None:
    """alacoffee grades body and acidity in words, not in points."""
    coffee = read("alacoffee")

    assert coffee.taste.body == 3
    assert coffee.taste.acidity == 4
    assert coffee.taste.scale_max == 5


def test_a_two_column_table_inside_the_description_is_read() -> None:
    """alegrecafe puts the lot in a <tr><td>label</td><td>value</td></tr> table."""
    coffee = read("alegrecafe")

    assert coffee.origin.region == "Kayanza"
    assert coffee.origin.farm == "Kibingo Washing Station"
    assert coffee.origin.altitude_min_m == 1700
    assert coffee.origin.variety == ["Red Bourbon"]
    assert coffee.roast.level is RoastLevel.LIGHT
    assert "med" in coffee.taste.flavor_notes


def test_label_and_value_siblings_without_a_colon_are_read() -> None:
    """cafegape writes <span>Země</span><strong>Peru</strong>, no colon anywhere."""
    coffee = read("cafegape")

    assert coffee.origin.country == "PE"
    assert coffee.origin.region == "Jaén, Cajamarca"
    assert coffee.origin.altitude_min_m == 1500
    assert coffee.origin.altitude_max_m == 1800
    assert coffee.processing.method is ProcessMethod.WASHED


def test_variant_weights_come_from_the_option_text() -> None:
    """birdsong names one axis ``Varianta``; the option text still states grams."""
    coffee = read("birdsong")

    assert [variant.weight_g for variant in coffee.variants] == [250, 500, 1000, 3000]
    assert [variant.price for variant in coffee.variants] == [408.0, 761.0, 1479.0, 4290.0]


def test_a_glyph_in_front_of_a_label_is_ignored() -> None:
    """kafista prefixes every parameter with ✓, which is decoration, not a word."""
    coffee = read("kafista")

    assert coffee.origin.country == "KE"
    assert coffee.roast.level is RoastLevel.MEDIUM_LIGHT
    assert coffee.roast.profile is RoastProfile.FILTER
    assert "citrusy" in coffee.taste.flavor_notes


def test_kafista_needs_no_label_map_of_its_own() -> None:
    """The glyph fix retired twelve overrides; the shared vocabulary does the work."""
    config = tomllib.loads((CONFIG_DIR / "kafista.toml").read_text("utf-8"))

    assert not config.get("label_map")


def test_a_shop_with_an_empty_basic_description_still_parses() -> None:
    """usteckakava's .basic-description says only that there is no description."""
    coffee = read("usteckakava")

    assert coffee.origin.country == "BI"
    assert coffee.origin.variety == ["Bourbon"]
    assert coffee.processing.method is ProcessMethod.WASHED
    assert coffee.roast.level is RoastLevel.MEDIUM
    assert coffee.taste.sca_score == 85.0


def test_a_pack_code_is_not_read_as_a_weight() -> None:
    """usteckakava's ``01-01-03/100`` sku is the 1 kg pack, not a 100 g one.

    Reading the code as grams claimed a tenth of the weight and ten times the
    price per kilogram, which is worse than admitting the weight is unknown.
    """
    coffee = read("usteckakava")
    by_price = {variant.price: variant.weight_g for variant in coffee.variants}

    assert by_price[235.0] == 250
    assert by_price[760.0] is None


def test_a_multi_brand_shop_records_the_roaster() -> None:
    """kofi resells other roasteries, so the brand is the roaster, not the shop."""
    coffee = read("kofi")

    assert coffee.raw_attributes["BRAND"] == "Concept coffee roasters"
    assert coffee.origin.country == "CO"
    assert coffee.origin.farm == "Peñas Blancas"
    assert coffee.taste.sca_score == 87.0


def test_no_shop_repeats_what_the_shared_vocabulary_already_knows() -> None:
    """An ordinary Czech or Slovak term belongs in labels.py, not in one shop."""
    repeated: list[str] = []
    for path in sorted(CONFIG_DIR.glob("*.toml")):
        config = tomllib.loads(path.read_text("utf-8"))
        if config.get("platform", "shoptet") != "shoptet":
            continue
        for label, field in (config.get("label_map") or {}).items():
            assert field in KNOWN_FIELDS, f"{path.name}: {label} points at {field!r}"
            if map_label(normalize.fold(label), DEFAULT_LABEL_MAP) == field:
                repeated.append(f"{path.name}: {label}")

    assert not repeated, f"move these into coffee_aggregator/labels.py: {repeated}"
