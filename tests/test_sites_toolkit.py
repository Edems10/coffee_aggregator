from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

import pytest
from bs4 import BeautifulSoup

from coffee_aggregator.http import FetchResult
from coffee_aggregator.labels import F_BODY, F_COUNTRY, F_ROAST
from coffee_aggregator.sites import toolkit as kit
from coffee_aggregator.sites.base import ProductRef

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coffee_aggregator.http import PoliteFetcher

ID_RE = re.compile(r"/detail/(?P<id>\d+)/")


class FakeFetcher:
    """Serves canned pages, optionally redirecting anything it does not know."""

    def __init__(self, bodies: dict[str, str], redirect_to: str | None = None) -> None:
        self.bodies = bodies
        self.redirect_to = redirect_to
        self.requested: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.requested.append(url)
        known = url in self.bodies
        final = url if known or self.redirect_to is None else self.redirect_to
        return FetchResult(url, final, 200, self.bodies.get(url, ""), from_cache=False, elapsed_s=0)

    def fetch_many(self, urls: Sequence[str]) -> list[FetchResult]:
        return [self.get(url) for url in urls]


def as_fetcher(fake: FakeFetcher) -> PoliteFetcher:
    return cast("PoliteFetcher", fake)


def refs_named(*names: str) -> list[ProductRef]:
    return [ProductRef(site_id="s", external_id=name, url=f"/{name}") for name in names]


# --------------------------------------------------------------------- payload


def test_a_broken_payload_costs_one_record_not_the_run() -> None:
    assert kit.json_object("{not json") == {}
    assert kit.json_object("[1, 2]") == {}
    assert kit.json_object('{"a": 1}') == {"a": 1}


def test_numbers_narrow_without_believing_a_boolean() -> None:
    assert kit.as_number(True) is None
    assert kit.as_number(7) == 7.0
    assert kit.as_number("7") is None


def test_the_first_product_of_a_data_layer_record_is_found() -> None:
    assert kit.first_record({"products": [{"id": 1}, {"id": 2}]}, "products") == {"id": 1}
    assert kit.first_record({"products": []}, "products") == {}
    assert kit.first_record("nonsense", "products") == {}


def test_a_localised_field_falls_back_to_any_language() -> None:
    assert kit.localised({"cs": "Káva", "en": "Coffee"}, "cs") == "Káva"
    assert kit.localised({"en": "Coffee"}, "cs") == "Coffee"
    assert kit.localised("plain", "cs") == "plain"
    assert kit.localised({}, "cs") is None


def test_shop_codes_survive_as_strings_whatever_type_they_arrive_as() -> None:
    record = {"EAN": 8_594_000_000_001, "code": " A1 ", "missing": None}
    assert kit.strings(record, ("EAN", "code", "missing")) == {
        "EAN": "8594000000001",
        "CODE": "A1",
    }


# ------------------------------------------------------------------------ refs


def test_an_id_is_read_out_of_the_url_rather_than_rebuilt() -> None:
    assert kit.id_from("https://x.sk/detail/668/kava", ID_RE) == "668"
    assert kit.id_from("https://x.sk/kosik/", ID_RE) is None
    assert kit.id_from(None, ID_RE) is None


def test_a_sitemap_yields_one_reference_per_distinct_product() -> None:
    xml = """<?xml version="1.0"?><urlset>
      <url><loc>https://x.sk/detail/1/a</loc></url>
      <url><loc>https://x.sk/detail/1/a?utm=1</loc></url>
      <url><loc>https://x.sk/o-nas</loc></url>
      <url><loc>https://x.sk/detail/2/b</loc></url>
    </urlset>"""
    refs = kit.sitemap_refs(xml, "shop", ID_RE)
    assert [ref.external_id for ref in refs] == ["1", "2"]
    assert all(ref.site_id == "shop" for ref in refs)


def test_a_currency_never_outlives_the_price_it_belongs_to() -> None:
    assert kit.product_ref("s", "1", "/1", price=9.5, currency="EUR").currency == "EUR"
    assert kit.product_ref("s", "1", "/1", currency="EUR").currency is None


# ------------------------------------------------------------------------ walk


def test_the_walk_stops_when_a_page_redirects_away() -> None:
    fake = FakeFetcher({"/1": "page one"}, redirect_to="/1")
    found = list(
        kit.walk_listing(as_fetcher(fake), ["/1", "/2", "/3"], lambda _text: refs_named("a"))
    )
    assert [ref.external_id for ref in found] == ["a"]
    assert fake.requested == ["/1", "/2"]


def test_a_paginated_walk_stops_at_the_first_page_that_adds_nothing() -> None:
    fake = FakeFetcher({"/1": "", "/2": "", "/3": ""})
    found = list(kit.walk_listing(as_fetcher(fake), ["/1", "/2", "/3"], lambda _t: refs_named("a")))
    assert [ref.external_id for ref in found] == ["a"]
    assert fake.requested == ["/1", "/2"]


def test_a_list_of_categories_keeps_walking_past_a_repeat() -> None:
    fake = FakeFetcher({"/1": "", "/2": "", "/3": ""})
    pages = iter([refs_named("a"), refs_named("a"), refs_named("b")])
    found = list(
        kit.walk_listing(
            as_fetcher(fake),
            ["/1", "/2", "/3"],
            lambda _text: next(pages),
            stop_when_stale=False,
        )
    )
    assert [ref.external_id for ref in found] == ["a", "b"]
    assert fake.requested == ["/1", "/2", "/3"]


# ----------------------------------------------------------------------- facts


def test_a_shop_overlay_wins_over_the_shared_vocabulary() -> None:
    label_map = kit.vocabulary({"lokalita": F_BODY})
    facts = kit.read_pairs([("Lokalita", "Gatara"), ("Pôvod", "Kolumbia")], label_map)
    assert facts.get(F_BODY) == "Gatara"
    assert facts.get(F_COUNTRY) == "Kolumbia"


def test_a_shop_gains_a_synonym_it_never_spelled_out() -> None:
    facts = kit.read_pairs([("Krajina pôvodu", "Kolumbia")], kit.vocabulary())
    assert facts.get(F_COUNTRY) == "Kolumbia"
    assert facts.raw == {"KRAJINA PÔVODU": "Kolumbia"}


def test_the_shop_s_own_spelling_stays_readable_for_the_rows_no_field_holds() -> None:
    facts = kit.read_pairs([("Chuť", "Čokoládová"), ("Charakteristika", "a - b")], kit.vocabulary())
    assert facts.pick("charakteristika") == "a - b"
    assert facts.pick("nothing", "chut") == "Čokoládová"
    assert facts.pick("nothing") is None


def test_an_implausible_value_stays_out_of_the_typed_field() -> None:
    facts = kit.read_pairs(
        [("Pražení", "Směs Arabiky a Robusty")],
        kit.vocabulary(),
    )
    assert facts.get(F_ROAST) is None
    assert facts.raw == {"PRAŽENÍ": "Směs Arabiky a Robusty"}


def test_a_bare_heading_claims_the_line_under_it_only_when_asked_to() -> None:
    lines = ["Pražení", "Světlé", "Tip našeho baristy", "Mlejte nahrubo."]
    with_headings, _ = kit.read_text(lines, kit.vocabulary())
    assert with_headings.get(F_ROAST) == "Světlé"
    without, prose = kit.read_text(lines, kit.vocabulary(), bare_labels=False)
    assert without.get(F_ROAST) is None
    assert prose == lines


def test_description_blocks_break_on_the_markup_not_on_the_text() -> None:
    soup = BeautifulSoup("<div><p>Pôvod: Kuba<br/>100 % Arabika</p></div>", "lxml")
    facts, prose = kit.read_blocks([soup.select_one("div")], kit.vocabulary())
    assert facts.get(F_COUNTRY) == "Kuba"
    assert prose == ["100 % Arabika"]


# ----------------------------------------------------------------------- build


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Skladom", True),
        ("Skladem (3 ks)", True),
        ("Není skladem", False),
        ("Vyprodáno", False),
        ("Vypredané", False),
        ("Nedostupné", False),
        ("", None),
        ("Doručíme do Vianoc", None),
    ],
)
def test_the_stock_wording_of_both_languages_is_read_the_same_way(
    text: str,
    expected: bool | None,
) -> None:
    assert kit.stock_state(text) is expected


def test_a_negation_wins_over_the_word_it_negates() -> None:
    assert kit.stock_state("Skladem", "Není skladem") is False


def test_a_schema_availability_is_in_stock_or_it_is_not() -> None:
    assert kit.schema_stock("https://schema.org/InStock") is True
    assert kit.schema_stock("https://schema.org/OutOfStock") is False
    assert kit.schema_stock(None) is None


def test_a_package_reads_its_weight_off_its_own_label() -> None:
    variant = kit.package(
        external_id="1",
        url="/1",
        label="Hmotnost: 250g",
        price=219.0,
        currency="CZK",
    )
    assert variant.weight_g == 250
    assert variant.currency == "CZK"


def test_a_package_without_a_price_states_no_currency() -> None:
    variant = kit.package(external_id="1", url="/1", label="1 kg", price=None, currency="CZK")
    assert variant.weight_g == 1000
    assert variant.currency is None


def test_a_stated_weight_beats_the_label() -> None:
    variant = kit.package(
        external_id="1",
        url="/1",
        label="velké balení",
        price=None,
        currency="CZK",
        weight_g=500,
    )
    assert variant.weight_g == 500


def test_page_metadata_only_ever_fills_a_gap() -> None:
    raw = {"KRAJINA": "Kuba"}
    kit.keep(raw, {"KRAJINA": "Brazílie", "OG_TITLE": "Kuba Serrano", "EMPTY": ""})
    assert raw == {"KRAJINA": "Kuba", "OG_TITLE": "Kuba Serrano"}


def test_a_gallery_drops_the_badges_and_the_repeats() -> None:
    urls = kit.gallery(
        "https://x.sk/",
        ["/img/a.jpg", "/img/a.jpg", "/images/gta/badge.png", None, "/img/b.jpg"],
        keep_when=lambda url: "/images/gta/" not in url,
    )
    assert urls == ["https://x.sk/img/a.jpg", "https://x.sk/img/b.jpg"]


# ------------------------------------------------------------------- microdata


MICRODATA = """
<div class="box" itemprop="aggregateRating">
  <span itemprop="ratingValue" content="4.7">4,7</span>
  <span itemprop="reviewCount">12</span>
  <span itemprop="bestRating" content="10"></span>
</div>
<ul id="reviews">
  <li itemprop="review">
    <span itemprop="author"><span itemprop="name">Jana</span></span>
    <meta itemprop="datePublished" content="2026-01-02"/>
    <div itemprop="reviewRating"><span itemprop="ratingValue" content="5">5</span></div>
    <p itemprop="description">Skvelá káva.</p>
  </li>
</ul>
"""


def test_an_aggregate_rating_is_read_with_the_scale_it_states() -> None:
    soup = BeautifulSoup(MICRODATA, "lxml")
    assert kit.ratings(soup.select_one("div.box")) == (4.7, 12, 10)


def test_an_unstated_scale_falls_back_to_five() -> None:
    soup = BeautifulSoup('<div><span itemprop="ratingValue">4</span></div>', "lxml")
    assert kit.ratings(soup.select_one("div")) == (4.0, None, 5)


def test_reviews_come_back_with_author_date_stars_and_text() -> None:
    soup = BeautifulSoup(MICRODATA, "lxml")
    reviews = kit.reviews(soup.select_one("#reviews"))
    assert len(reviews) == 1
    assert reviews[0].author == "Jana"
    assert reviews[0].rating == 5.0
    assert reviews[0].text == "Skvelá káva."
    assert reviews[0].date is not None
    assert reviews[0].date.isoformat() == "2026-01-02"


def test_a_page_with_no_reviews_returns_none_of_them() -> None:
    assert kit.reviews(None) == []
    assert kit.reviews(BeautifulSoup("<div></div>", "lxml")) == []
