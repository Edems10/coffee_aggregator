from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from coffee_aggregator import normalize
from coffee_aggregator.labels import plausible_weight, stated_weight
from coffee_aggregator.models import RoastLevel
from coffee_aggregator.sites import get as get_site
from coffee_aggregator.sites import load_all
from coffee_aggregator.sites.base import ProductRef

if TYPE_CHECKING:
    from coffee_aggregator.models import Coffee

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
#: How a fixture directory names the platform in front of the shop's registry id.
PLATFORM_PREFIXES = ("woo_", "shoptet_", "shopify_")
#: The sensory bars, which all share one scale.
BARS = ("body", "bitterness", "acidity", "sweetness")
MIN_SCA = 50.0
MAX_SCA = 100.0
FULL_PERCENT = 100


def site_id_of(directory: str) -> str:
    """Return the registry id a fixture directory belongs to."""
    for prefix in PLATFORM_PREFIXES:
        if directory.startswith(prefix):
            return directory.removeprefix(prefix)
    return directory


def _payload_ref(site_id: str, base_url: str, item: dict[str, Any]) -> ProductRef:
    return ProductRef(
        site_id=site_id,
        external_id=str(item.get("id") or item.get("handle") or ""),
        url=str(item.get("permalink") or base_url),
        payload=json.dumps(item, ensure_ascii=False),
    )


def parse_fixture_dir(directory: Path) -> list[tuple[str, Coffee]]:
    """Parse every product a fixture directory holds, whatever shape it is in.

    A shop's fixtures arrive as detail pages, as a Store API page or as a
    Shopify collection page, and every adapter parses a payload a reference
    carries in preference to a page body. Reading all three here is what lets a
    new shop be covered by these invariants the moment its fixture lands, with
    no test change at all.
    """
    load_all()
    site_id = site_id_of(directory.name)
    site = get_site(site_id)
    parsed: list[tuple[str, Coffee]] = []
    for path in sorted(directory.glob("detail_*.html")):
        ref = ProductRef(site_id=site_id, external_id="", url=f"{site.base_url}#{path.stem}")
        coffee = site.parse_product(path.read_text("utf-8"), ref)
        if coffee is not None:
            parsed.append((path.name, coffee))
    for path in sorted(directory.glob("*products*.json")):
        document = json.loads(path.read_text("utf-8"))
        items = document["products"] if isinstance(document, dict) else document
        for item in items:
            coffee = site.parse_product("", _payload_ref(site_id, site.base_url, item))
            if coffee is not None:
                parsed.append((f"{path.name}:{item.get('id')}", coffee))
    return parsed


def _dirs() -> list[str]:
    return sorted(
        path.name
        for path in FIXTURE_ROOT.iterdir()
        if path.is_dir() and (any(path.glob("detail_*.html")) or any(path.glob("*products*.json")))
    )


@pytest.fixture(scope="session")
def _cache() -> dict[str, list[tuple[str, Coffee]]]:
    return {}


@pytest.fixture
def shop(
    request: pytest.FixtureRequest,
    _cache: dict[str, list[tuple[str, Coffee]]],
) -> list[tuple[str, Coffee]]:
    name = request.param
    if name not in _cache:
        _cache[name] = parse_fixture_dir(FIXTURE_ROOT / name)
    return _cache[name]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "shop" in metafunc.fixturenames:
        metafunc.parametrize("shop", _dirs(), indirect=True, ids=_dirs())


# --------------------------------------------------------------- the invariants


def test_every_fixture_directory_parses_at_least_one_coffee(
    shop: list[tuple[str, Coffee]],
) -> None:
    assert shop, "the directory holds fixtures but no product parsed out of them"


def test_a_weight_stated_in_the_name_is_the_weight_stored(
    shop: list[tuple[str, Coffee]],
) -> None:
    # "Brasil Santos 1 kg" and a stored 250 g cannot both be right, and it is
    # price_per_kg — the number every product is ranked on — that goes wrong.
    for label, coffee in shop:
        stated = stated_weight(coffee.name)
        if stated is None:
            continue
        assert coffee.weight_g == stated, f"{label}: {coffee.name!r} states {stated} g"


def test_a_roast_level_never_contradicts_the_text_it_came_from(
    shop: list[tuple[str, Coffee]],
) -> None:
    # A shop that writes "Tmavé" and a record that says light is worse than no
    # record at all: roast is the one field a drinker sorts on.
    for label, coffee in shop:
        from_raw = normalize.normalize_roast_level(coffee.roast.raw)
        if from_raw is RoastLevel.UNKNOWN:
            continue
        assert coffee.roast.level is from_raw, f"{label}: {coffee.roast.raw!r} is {from_raw}"


def test_a_stored_weight_is_a_bag_of_coffee(shop: list[tuple[str, Coffee]]) -> None:
    # A pack code, an EAN fragment or a parcel weight read as the bag size is
    # how a 1 g and a 24 000 g coffee got into the catalogue.
    for label, coffee in shop:
        if coffee.weight_g is not None:
            assert plausible_weight(coffee.weight_g), f"{label}: {coffee.weight_g} g"
        for variant in coffee.variants:
            if variant.weight_g is not None:
                assert plausible_weight(variant.weight_g), f"{label}: {variant.label!r}"


def test_a_currency_is_stated_exactly_when_a_price_is(shop: list[tuple[str, Coffee]]) -> None:
    # A currency without a price is a claim about nothing, and a price without
    # one cannot be converted, so neither may appear alone.
    for label, coffee in shop:
        assert (coffee.price is None) == (coffee.currency is None), label
        for variant in coffee.variants:
            assert (variant.price is None) == (variant.currency is None), f"{label}: {variant}"


def test_sensory_bars_stay_on_their_own_scale(shop: list[tuple[str, Coffee]]) -> None:
    for label, coffee in shop:
        for bar in BARS:
            value = getattr(coffee.taste, bar)
            if value is not None:
                assert 0 <= value <= coffee.taste.scale_max, f"{label}: {bar}={value}"


def test_a_cupping_score_stays_inside_the_sca_range(shop: list[tuple[str, Coffee]]) -> None:
    for label, coffee in shop:
        score = coffee.taste.sca_score
        if score is not None:
            assert MIN_SCA <= score <= MAX_SCA, f"{label}: {score}"


def test_an_altitude_range_runs_upwards(shop: list[tuple[str, Coffee]]) -> None:
    for label, coffee in shop:
        low, high = coffee.origin.altitude_min_m, coffee.origin.altitude_max_m
        if low is not None and high is not None:
            assert low <= high, f"{label}: {low}-{high}"


def test_flavour_notes_are_a_clean_list(shop: list[tuple[str, Coffee]]) -> None:
    for label, coffee in shop:
        notes = coffee.taste.flavor_notes
        assert all(note.strip() for note in notes), f"{label}: {notes}"
        assert len(set(notes)) == len(notes), f"{label}: {notes}"


def test_a_variety_is_never_the_species(shop: list[tuple[str, Coffee]]) -> None:
    # "100 % Arabica" is a species statement; stored as a variety it pollutes
    # every grouping by cultivar.
    for label, coffee in shop:
        for variety in coffee.origin.variety:
            folded = normalize.fold(variety)
            assert "arabi" not in folded, f"{label}: {variety!r}"
            assert "robus" not in folded, f"{label}: {variety!r}"


def test_a_species_split_adds_up(shop: list[tuple[str, Coffee]]) -> None:
    for label, coffee in shop:
        arabica, robusta = coffee.species.arabica_pct, coffee.species.robusta_pct
        if arabica is not None and robusta is not None:
            assert arabica + robusta == FULL_PERCENT, f"{label}: {arabica}/{robusta}"


def test_a_name_that_says_decaf_is_stored_as_decaf(shop: list[tuple[str, Coffee]]) -> None:
    for label, coffee in shop:
        folded = normalize.fold(coffee.name)
        if "bezkofein" in folded or "decaf" in folded:
            assert coffee.decaf is True, f"{label}: {coffee.name!r}"
