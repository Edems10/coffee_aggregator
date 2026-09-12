from __future__ import annotations

from datetime import date

import pytest

from coffee_aggregator import normalize
from coffee_aggregator.models import ProcessMethod, RoastLevel, RoastProfile
from coffee_aggregator.normalize import (
    detect_blend,
    detect_country,
    detect_currency,
    normalize_process,
    normalize_roast_level,
    normalize_roast_profile,
    parse_altitude,
    parse_date_dmy,
    parse_float,
    parse_int,
    parse_price,
    parse_species,
    parse_weight_grams,
    split_list,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("200 g", 200),
        ("250g", 250),
        ("1 kg", 1000),
        ("0,5 kg", 500),
        ("0.25 kg", 250),
        ("Hmotnosť: 1000 g", 1000),
        ("Balení 500 gramů", 500),
        ("1 kilogram", 1000),
        ("bez hmotnosti", None),
        ("", None),
        (None, None),
        ("---", None),
    ],
)
def test_parse_weight_grams(text: str | None, expected: int | None) -> None:
    assert parse_weight_grams(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9,99 €", (9.99, "EUR")),
        ("249 Kč", (249.0, "CZK")),
        ("1 299 Kč", (1299.0, "CZK")),
        ("1 299 Kč", (1299.0, "CZK")),
        ("249,-", (249.0, None)),
        ("9.99", (9.99, None)),
        ("12,50 EUR", (12.5, "EUR")),
        ("599 CZK", (599.0, "CZK")),
        ("cena na dotaz", (None, None)),
        (None, (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_price(text: str | None, expected: tuple[float | None, str | None]) -> None:
    assert parse_price(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("100 Kč", "CZK"), ("5 €", "EUR"), ("nic", None), (None, None)],
)
def test_detect_currency(text: str | None, expected: str | None) -> None:
    assert detect_currency(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1500 - 1700 m n. m.", (1500, 1700)),
        ("1 200 m.n.m.", (1200, 1200)),
        ("1650m", (1650, 1650)),
        ("1400–1900 masl", (1400, 1900)),
        ("1200 až 1400 m", (1200, 1400)),
        ("nadmorská výška neuvedená", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_altitude(text: str | None, expected: tuple[int | None, int | None]) -> None:
    assert parse_altitude(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("praná", ProcessMethod.WASHED),
        ("mytá", ProcessMethod.WASHED),
        ("washed", ProcessMethod.WASHED),
        ("natural", ProcessMethod.NATURAL),
        ("suchá metóda", ProcessMethod.NATURAL),
        ("přírodní", ProcessMethod.NATURAL),
        ("sušená", ProcessMethod.NATURAL),
        ("honey", ProcessMethod.HONEY),
        ("medová", ProcessMethod.HONEY),
        ("anaeróbna fermentácia", ProcessMethod.ANAEROBIC),
        ("anaerobní", ProcessMethod.ANAEROBIC),
        ("wet hulled", ProcessMethod.WET_HULLED),
        ("giling basah", ProcessMethod.WET_HULLED),
        ("pulped natural", ProcessMethod.PULPED_NATURAL),
        ("carbonic maceration", ProcessMethod.EXPERIMENTAL),
        ("experimentálna", ProcessMethod.EXPERIMENTAL),
        ("úplne iné slovo", ProcessMethod.OTHER),
        ("", ProcessMethod.UNKNOWN),
        (None, ProcessMethod.UNKNOWN),
    ],
)
def test_normalize_process(text: str | None, expected: ProcessMethod) -> None:
    assert normalize_process(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("svetlé", RoastLevel.LIGHT),
        ("světlé", RoastLevel.LIGHT),
        ("light", RoastLevel.LIGHT),
        ("City", RoastLevel.LIGHT),
        ("Cinnamon", RoastLevel.LIGHT),
        ("svetlo-stredné", RoastLevel.MEDIUM_LIGHT),
        ("světle střední", RoastLevel.MEDIUM_LIGHT),
        ("City+", RoastLevel.MEDIUM),
        ("stredné", RoastLevel.MEDIUM),
        ("střední", RoastLevel.MEDIUM),
        ("medium", RoastLevel.MEDIUM),
        ("Full City", RoastLevel.MEDIUM_DARK),
        ("Full City+", RoastLevel.DARK),
        ("tmavé", RoastLevel.DARK),
        ("dark", RoastLevel.DARK),
        ("French", RoastLevel.DARK),
        ("Italian", RoastLevel.DARK),
        ("???", RoastLevel.UNKNOWN),
        (None, RoastLevel.UNKNOWN),
    ],
)
def test_normalize_roast_level(text: str | None, expected: RoastLevel) -> None:
    assert normalize_roast_level(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("espresso", RoastProfile.ESPRESSO),
        ("na pákový kávovar", RoastProfile.ESPRESSO),
        ("filter", RoastProfile.FILTER),
        ("překapávaná káva", RoastProfile.FILTER),
        ("V60 / Chemex", RoastProfile.FILTER),
        ("omni", RoastProfile.OMNI),
        ("espresso aj filter", RoastProfile.OMNI),
        ("nič", RoastProfile.UNKNOWN),
        (None, RoastProfile.UNKNOWN),
    ],
)
def test_normalize_roast_profile(text: str | None, expected: RoastProfile) -> None:
    assert normalize_roast_profile(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("brazílska káva", "BR"),
        ("brazilská zrnková káva", "BR"),
        ("kubánska Serrano", "CU"),
        ("etiópska Yirgacheffe", "ET"),
        ("Kolumbijská Supremo", "CO"),
        ("keňská AA", "KE"),
        ("Vietnam Robusta", "VN"),
        ("Sumatra Mandheling", "ID"),
        ("Costa Rica Tarrazu", "CR"),
        ("Blue Mountain", "JM"),
        ("Guatemala Huehuetenango", "GT"),
        ("žiadna krajina", None),
        ("", None),
        (None, None),
    ],
)
def test_detect_country(text: str | None, expected: str | None) -> None:
    assert detect_country(text) == expected


def test_detect_country_table_covers_at_least_35_countries() -> None:
    assert len(normalize._COUNTRY_TERMS) >= 35


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90 % Arabika, 10 % Robusta", (90, 10)),
        ("100% arabica", (100, 0)),
        ("80% robusta", (20, 80)),
        ("Arabica: 70 %", (70, 30)),
        ("nič o druhu", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_species(text: str | None, expected: tuple[int | None, int | None]) -> None:
    assert parse_species(text) == expected


@pytest.mark.parametrize(
    ("text", "arabica", "robusta", "expected"),
    [
        ("Espresso zmes", None, None, True),
        ("Směs do espressa", None, None, True),
        ("Kuba Serrano", 100, 0, False),
        ("Nieco", 80, 20, True),
        (None, None, None, False),
    ],
)
def test_detect_blend(
    text: str | None,
    arabica: int | None,
    robusta: int | None,
    expected: bool,
) -> None:
    assert detect_blend(text, arabica, robusta) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("kakao, karamel / tabakové listy", ["kakao", "karamel", "tabakové listy"]),
        ("citrus; med", ["citrus", "med"]),
        ("kakao, Kakao", ["kakao"]),
        ("", []),
        (None, []),
    ],
)
def test_split_list(text: str | None, expected: list[str]) -> None:
    assert split_list(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("11.09.2026", date(2026, 9, 11)),
        ("11. 9. 2026", date(2026, 9, 11)),
        ("2026-09-11", date(2026, 9, 11)),
        ("31.02.2026", None),
        ("nikdy", None),
        (None, None),
    ],
)
def test_parse_date_dmy(text: str | None, expected: date | None) -> None:
    assert parse_date_dmy(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("51 hodnotení", 51), ("Upražené a vypité: 24 339x", 24339), ("nic", None), (None, None)],
)
def test_parse_int(text: str | None, expected: int | None) -> None:
    assert parse_int(text) == expected


@pytest.mark.parametrize(("text", "expected"), [("4,8 / 5", 4.8), ("nic", None), (None, None)])
def test_parse_float(text: str | None, expected: float | None) -> None:
    assert parse_float(text) == expected


# --- intensity words share the 1-5 scale with the drawn bars ----------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("žiadna", 1),
        ("žádná", 1),
        ("none", 1),
        ("nízka", 2),
        ("nízká", 2),
        ("low", 2),
        ("jemná", 2),
        ("stredná", 3),
        ("střední", 3),
        ("medium", 3),
        ("vysoká", 4),
        ("high", 4),
        ("plná", 4),
        ("plné telo", 4),
        ("full", 4),
        ("výrazná", 4),
        ("veľmi vysoká", 5),
        ("velmi vysoká", 5),
        ("very high", 5),
        ("intenzívna", 5),
        ("4", 4),
        ("3 z 5", 3),
    ],
)
def test_parse_intensity_maps_words_onto_the_bar_scale(text: str, expected: int) -> None:
    assert normalize.parse_intensity(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "čokoláda a oriešky",
        "príjemná",  # contains "jemná" but means pleasant
        "príjemné, vyvážené telo,",
        "9",
    ],
)
def test_parse_intensity_returns_none_for_anything_else(text: str | None) -> None:
    assert normalize.parse_intensity(text) is None


# --- variety cleanup --------------------------------------------------------


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        (["Arabica – Lempira"], ["Lempira"]),
        (["Arabica - Mundo Novo"], ["Mundo Novo"]),
        (["arabika Bourbon"], ["Bourbon"]),
        (["Robusta Conilon"], ["Conilon"]),
        (["Castillo / Caturra"], ["Castillo", "Caturra"]),
        (["SL28, SL34 · Ruiru 11"], ["SL28", "SL34", "Ruiru 11"]),
        (["Typica", "typica"], ["Typica"]),
        ([], []),
        (None, []),
        (["Arabica"], []),
    ],
)
def test_clean_variety_strips_species_and_splits(
    items: list[str] | None,
    expected: list[str],
) -> None:
    assert normalize.clean_variety(items) == expected


# --- multi-method processing -------------------------------------------------


def test_parse_processing_keeps_every_method_a_lot_names() -> None:
    processing = normalize.parse_processing("Washed · Natural")
    assert processing.method is ProcessMethod.MIXED
    assert processing.methods == [ProcessMethod.WASHED, ProcessMethod.NATURAL]
    assert processing.raw == "Washed · Natural"


def test_parse_processing_reads_one_method_as_itself() -> None:
    processing = normalize.parse_processing("natural / ruční sběr")
    assert processing.method is ProcessMethod.NATURAL
    assert processing.methods == [ProcessMethod.NATURAL]


def test_parse_processing_does_not_split_the_stages_of_one_method() -> None:
    """ "mokré, sušené na slnku" is a washed coffee that was sun dried."""
    processing = normalize.parse_processing("mokré, sušené na slnku")
    assert processing.method is ProcessMethod.WASHED
    assert processing.methods == [ProcessMethod.WASHED]


@pytest.mark.parametrize(
    ("text", "method"),
    [
        (None, ProcessMethod.UNKNOWN),
        ("", ProcessMethod.UNKNOWN),
        ("nějaký vlastní postup", ProcessMethod.OTHER),
    ],
)
def test_parse_processing_is_total(text: str | None, method: ProcessMethod) -> None:
    processing = normalize.parse_processing(text)
    assert processing.method is method
    assert processing.methods == []
