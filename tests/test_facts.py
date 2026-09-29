from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coffee_aggregator import labels as facts
from coffee_aggregator import normalize
from coffee_aggregator.labels import (
    F_BREWING,
    F_COUNTRY,
    F_FLAVOR,
    F_PROCESS,
    F_ROAST,
    F_SPECIES,
    F_WEIGHT,
    Labels,
    bare_label,
    build_map,
    headline_weight,
    map_label,
    option_list,
    plausible,
    read_lines,
    stated_weight,
)
from coffee_aggregator.models import Variant
from coffee_aggregator.sites import html as dom

PACKAGE = Path(facts.__file__).parent
MAP = build_map(facts.TERMS)


def mapped(label: str, value: str) -> dict[str, str]:
    collected = Labels()
    collected.add(label, value, MAP)
    return collected.by_field


# --- the package stays a string reader ---------------------------------------


@pytest.mark.parametrize("module", sorted(PACKAGE.glob("*.py")), ids=lambda path: path.name)
def test_the_vocabulary_package_never_imports_bs4(module: Path) -> None:
    """The shared reader takes strings; a Tag in here would drag the DOM back in."""
    tree = ast.parse(module.read_text("utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "bs4" not in imported
    assert "lxml" not in imported


def test_the_label_pattern_matches_the_dom_readers_copy() -> None:
    """Two copies exist because only one of the two files may import bs4."""
    assert facts.LABEL_RE.pattern == dom.LABEL_RE.pattern


# --- map_label ----------------------------------------------------------------


def test_a_label_matches_exactly_before_it_matches_by_words() -> None:
    assert map_label("cupping score", {"cupping": "x", "cupping score": "sca_score"}) == "sca_score"


def test_a_label_matches_on_whole_words_only() -> None:
    assert map_label(normalize.fold("Stupeň pražení"), MAP) == F_ROAST
    assert map_label(normalize.fold("Nadmořská výška"), MAP) == "altitude"


def test_a_sentence_never_matches() -> None:
    long_label = normalize.fold("Objednávky nad 1 kg zasíláme zdarma po celé ČR")

    assert map_label(long_label, MAP) is None


def test_an_ignored_term_maps_onto_the_empty_field() -> None:
    assert map_label(normalize.fold("Kategorie"), MAP) == ""


# --- plausible ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("field_name", "value", "expected"),
    [
        (F_WEIGHT, "250 g", True),
        (F_WEIGHT, "3 g", False),
        (F_WEIGHT, "25 kg", False),
        (F_PROCESS, "washed", True),
        (F_PROCESS, "káva se zpracovává tradiční metodou na našich farmách v Brazílii", False),
        (F_COUNTRY, "Kolumbie", True),
        (F_COUNTRY, "z našich farem", False),
        # the roast rule: a level, a style, or nothing at all
        (F_ROAST, "světlé", True),
        (F_ROAST, "Espresso", True),
        (F_ROAST, "Omniroast", True),
        (F_ROAST, "Druh: Směs Arabiky a Robusty", False),
        (
            F_ROAST,
            "Kávu pražíme v malých dávkách na bubnové pražičce a každou šarži cupujeme",
            False,
        ),
    ],
)
def test_plausible_gates_the_fields_with_an_obvious_shape(
    field_name: str,
    value: str,
    *,
    expected: bool,
) -> None:
    assert plausible(field_name, value) is expected


def test_an_implausible_roast_stays_out_of_the_typed_field() -> None:
    """A fuzzy "pražení" hit on a species row used to store a dark roast for a light coffee."""
    assert mapped("Stupeň pražení", "Druh: Směs Arabiky a Robusty") == {}
    assert mapped("Stupeň pražení", "světlé") == {F_ROAST: "světlé"}


# --- the five label shapes ----------------------------------------------------

#: The same fact, written the five ways the three platforms actually meet it.
SHAPES = {
    "one line": ["Pražení: Světlé"],
    "label on its own line": ["Pražení:", "Světlé"],
    "label with no colon": ["Pražení", "Světlé"],
    "label and a following paragraph": ["Pražení:", "Světlé", "Káva z vysočiny."],
    "decorated label": ["• Pražení:", "Světlé"],
}


@pytest.mark.parametrize("shape", sorted(SHAPES), ids=sorted(SHAPES))
def test_every_label_shape_reaches_the_roast_field(shape: str) -> None:
    pairs, _prose = read_lines(SHAPES[shape], MAP)
    collected = Labels()
    for label, value in pairs:
        collected.add(label, value, MAP)

    assert collected.get(F_ROAST) == "Světlé"


def test_a_bare_label_never_swallows_the_next_fact() -> None:
    """longberry prints one heading over a run of labelled lines."""
    pairs, prose = read_lines(
        ["Chuťový profil", "Region/oblast: Santander", "Praženie: Espresso"],
        MAP,
    )

    assert ("Region/oblast", "Santander") in pairs
    assert ("Praženie", "Espresso") in pairs
    assert prose == ["Chuťový profil"]


def test_a_bare_label_never_swallows_another_bare_label() -> None:
    pairs, prose = read_lines(["Arabika 60%", "Robusta 40%"], MAP)

    assert pairs == []
    assert prose == ["Arabika 60%", "Robusta 40%"]


def test_a_paragraph_is_too_long_to_be_a_bare_labels_value() -> None:
    paragraph = "Túto kávu pražíme pomaly, " * 6
    pairs, prose = read_lines(["Pôvod", paragraph], MAP)

    assert pairs == []
    assert prose == ["Pôvod", paragraph]


def test_an_ordinary_sentence_is_not_a_bare_label() -> None:
    assert not bare_label("Dnes pražíme každý pondelok", MAP)
    assert bare_label("Chuťový profil", MAP)


def test_a_platform_may_bring_its_own_single_line_reader() -> None:
    def pipes(line: str, label_map: dict[str, str]) -> list[tuple[str, str]]:
        del label_map
        return [
            (part.split(":", 1)[0].strip(), part.split(":", 1)[1].strip())
            for part in line.split("|")
            if ":" in part
        ]

    pairs, prose = read_lines(["Region: Huila | Odrůda: Caturra"], MAP, pipes)

    assert pairs == [("Region", "Huila"), ("Odrůda", "Caturra")]
    assert prose == []


# --- headline_weight ----------------------------------------------------------


def variant(weight: int | None, price: float | None) -> Variant:
    return Variant(url="https://example.sk/p", weight_g=weight, price=price, label=None)


def weighed(
    label: str | None = None,
    name: str = "Brasil",
    variants: list[Variant] | None = None,
    price: float | None = None,
    fallback: str | None = None,
) -> int | None:
    collected = Labels()
    if label is not None:
        collected.by_field[F_WEIGHT] = label
    return headline_weight(collected, name, variants or [], price=price, fallback=fallback)


def test_a_stated_weight_beats_every_variant() -> None:
    assert weighed(label="1 kg", variants=[variant(250, 9.0)]) == 1000


def test_a_size_axis_is_not_a_stated_weight() -> None:
    """ripit.sk lists "1000 g, 500 g, 250 g" in one attribute and prices the 250 g."""
    assert weighed(label="1000 g, 500 g, 250 g", variants=[variant(250, 9.0)]) == 250


def test_the_name_beats_an_inferred_weight() -> None:
    """A Shoptet "Brasil 1000 g" offering 250 g and 1000 g sells the 1000 g bag."""
    weight = weighed(
        name="Brasil 1000 g",
        variants=[variant(250, 9.0), variant(1000, 30.0)],
        price=9.0,
    )

    assert weight == 1000


def test_a_name_that_states_two_sizes_states_none() -> None:
    assert weighed(name="Sada 250 g + 1 kg", variants=[variant(250, 9.0)], price=9.0) == 250


def test_the_variant_matching_the_headline_price_wins_over_document_order() -> None:
    weight = weighed(
        variants=[variant(1000, 30.0), variant(250, 9.0)],
        price=9.0,
    )

    assert weight == 250


def test_the_cheapest_variant_settles_it_when_no_price_matches() -> None:
    weight = weighed(variants=[variant(1000, 30.0), variant(250, 9.0)], price=None)

    assert weight == 250


def test_an_unpriced_ladder_falls_back_to_the_smallest_size() -> None:
    weight = weighed(variants=[variant(1000, None), variant(250, None)])

    assert weight == 250


def test_an_implausible_variant_weight_is_never_believed() -> None:
    """A Shopify ``grams`` of 1 is a merchant's typo, not a bag of coffee."""
    assert weighed(variants=[variant(1, 9.0)], price=9.0) is None


def test_the_fallback_is_the_last_resort() -> None:
    assert weighed(fallback="300 g") == 300
    assert weighed(name="Brasil 250 g", fallback="300 g") == 250


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("250 g", 250),
        ("1 kg", 1000),
        ("0,25 kg", 250),
        ("250 g, 1 kg", None),
        ("250 g, 250g", 250),
        ("3 g", None),
        ("", None),
        (None, None),
    ],
)
def test_stated_weight_reads_one_size_and_refuses_a_list(
    text: str | None, expected: int | None
) -> None:
    assert stated_weight(text) == expected


# --- the value readers --------------------------------------------------------


def test_an_option_list_loses_the_surcharge_the_shop_prints() -> None:
    assert option_list("Espresso +10 Kč, Filtr +5 €") == ["Espresso", "Filtr"]


def test_cup_notes_are_read_from_a_list_and_not_from_prose() -> None:
    collected = Labels()
    collected.by_field[F_FLAVOR] = "Citrusy • Sušená slivka • Tmavé kakao"
    taste = facts.parse_taste(collected, None)

    assert taste.flavor_notes == ["Citrusy", "Sušená slivka", "Tmavé kakao"]


def test_a_sentence_in_the_flavour_row_is_tasting_text_only() -> None:
    collected = Labels()
    collected.by_field[F_FLAVOR] = "Káva chutná po citrusech a má dlouhou dochuť."
    taste = facts.parse_taste(collected, None)

    assert taste.flavor_notes == []
    assert taste.tasting_text


def test_the_species_split_is_read_from_its_own_row() -> None:
    collected = Labels()
    collected.by_field[F_SPECIES] = "80 % Arabica / 20 % Robusta"
    species = facts.parse_species(collected, "House Blend")

    assert (species.arabica_pct, species.robusta_pct) == (80, 20)
    assert species.is_blend


def test_brewing_methods_split_into_a_list() -> None:
    collected = Labels()
    collected.by_field[F_BREWING] = "Espresso, V60, French press"

    assert facts.parse_taste(collected, None).brewing_methods == ["Espresso", "V60", "French press"]
