from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from html import unescape
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup, Tag

from coffee_aggregator import normalize
from coffee_aggregator.models import Coffee, Popularity, Variant
from coffee_aggregator.platforms.shoptet import (
    F_ACIDITY,
    F_ALTITUDE,
    F_BITTERNESS,
    F_BODY,
    F_BREWING,
    F_CERTIFICATIONS,
    F_COUNTRY,
    F_DECAF,
    F_FARM,
    F_FLAVOR,
    F_HARVEST,
    F_IGNORE,
    F_PROCESS,
    F_PRODUCER,
    F_REGION,
    F_ROAST,
    F_SCA,
    F_SPECIES,
    F_STATION,
    F_SWEETNESS,
    F_VARIETY,
    F_WEIGHT,
    KNOWN_FIELDS,
    _available,
    _is_decaf,
    _Labels,
    _option_list,
    _parse_origin,
    _parse_roast,
    _parse_species,
    _parse_taste,
    _score,
    _specialty_grade,
)
from coffee_aggregator.sites import html as dom
from coffee_aggregator.sites.base import DEFAULT_IGNORED, ProductRef, SiteAdapter

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from coffee_aggregator.http import PoliteFetcher

logger = logging.getLogger(__name__)

PLATFORM: Final = "woocommerce"
DEFAULT_MAX_PAGES: Final = 20
#: The Store API's own ceiling; asking for more is rejected with a 400.
DEFAULT_PER_PAGE: Final = 100
MAX_PER_PAGE: Final = 100
STORE_PRODUCTS_PATH: Final = "wp-json/wc/store/v1/products"
STORE_CATEGORIES_PATH: Final = "wp-json/wc/store/v1/products/categories"
#: ``currency_minor_unit`` is a small exponent; anything else is a broken shop.
_MAX_MINOR_UNIT: Final = 6
#: Category ids a synthetic-taxonomy plugin injects (``caffe4u.sk`` ships
#: 16-digit ones beside the real terms); a WordPress term id is never this big.
_MAX_REAL_CATEGORY_ID: Final = 10**9

#: Folded CZ/SK/EN attribute name -> the field it feeds, built from the Store
#: API survey of 39 Czech and Slovak WooCommerce roasteries (frequencies in
#: ``woo_survey/label_union.json``). It is deliberately *not* Shoptet's map:
#: WooCommerce shops use "Hmotnosť" for the bag of beans, where Shoptet uses it
#: for the parcel, and the Slovak spellings dominate this cohort.
DEFAULT_LABEL_MAP: Final[dict[str, str]] = {
    # decoys: merchandise and shop mechanics that merely contain a real key
    "doplnky": F_IGNORE,
    "velkost drippera": F_IGNORE,
    "potlac": F_IGNORE,
    "rozmer": F_IGNORE,
    "balicek": F_IGNORE,
    "skladovanie": F_IGNORE,
    "uskladneni": F_IGNORE,
    "storage": F_IGNORE,
    "kategorie": F_IGNORE,
    "kategoria": F_IGNORE,
    "podla oblasti": F_IGNORE,  # a shop menu ("Kávy z Afriky"), not a region
    "ean": F_IGNORE,
    # origin
    "krajina povodu": F_COUNTRY,
    "zeme puvodu": F_COUNTRY,
    "krajina": F_COUNTRY,
    "povod": F_COUNTRY,
    "puvod": F_COUNTRY,
    "povod kavy": F_COUNTRY,
    "country of origin": F_COUNTRY,
    "country": F_COUNTRY,
    "origin": F_COUNTRY,
    "region": F_REGION,
    "oblast": F_REGION,
    "lokalita": F_REGION,
    "location": F_REGION,
    "farma": F_FARM,
    "farm": F_FARM,
    "finca": F_FARM,
    "plantaz": F_FARM,
    "farmar": F_PRODUCER,
    "farmari": F_PRODUCER,
    "farmar / plantaz": F_PRODUCER,
    "producent": F_PRODUCER,
    "producer": F_PRODUCER,
    "pestovatel": F_PRODUCER,
    "spracovatel": F_PRODUCER,
    "majitel": F_PRODUCER,
    "vyrobil": F_PRODUCER,
    "spracovatelska stanica": F_STATION,
    "zpracovatelska stanice": F_STATION,
    "washing station": F_STATION,
    "nadmorska vyska": F_ALTITUDE,
    "altitude": F_ALTITUDE,
    "vyska": F_ALTITUDE,
    "masl": F_ALTITUDE,
    "odroda": F_VARIETY,
    "odruda": F_VARIETY,
    "odroda kavy": F_VARIETY,
    "odroda alebo varieta": F_VARIETY,
    "varieta": F_VARIETY,
    "variety": F_VARIETY,
    "varieties": F_VARIETY,
    "kultivar": F_VARIETY,
    "zber": F_HARVEST,
    "sber": F_HARVEST,
    "sklizen": F_HARVEST,
    "obdobie zberu": F_HARVEST,
    "uroda": F_HARVEST,
    "harvest": F_HARVEST,
    # processing and roast
    "spracovanie": F_PROCESS,
    "zpracovani": F_PROCESS,
    "sposob spracovania": F_PROCESS,
    "process": F_PROCESS,
    "processing": F_PROCESS,
    "prazenie": F_ROAST,
    "prazeni": F_ROAST,
    "stupen prazenia": F_ROAST,
    "stupen prazeni": F_ROAST,
    "typ prazenia": F_ROAST,
    "typ prazeni": F_ROAST,
    "sposob prazenia": F_ROAST,
    "roast": F_ROAST,
    # brewing: what the shop says to make with it, and how it will grind it
    "priprava": F_BREWING,
    "pripravu": F_BREWING,
    "priprava kavy": F_BREWING,
    "odporucana priprava": F_BREWING,
    "odporucany sposob pripravy": F_BREWING,
    "sposoby pripravy": F_BREWING,
    "podla pripravy kavy": F_BREWING,
    "preparation method": F_BREWING,
    "vhodne pro": F_BREWING,
    "vhodna pro": F_BREWING,
    "vhodne na": F_BREWING,
    "pouzitie": F_BREWING,
    "pouziti": F_BREWING,
    "brewing": F_BREWING,
    "mletie": F_BREWING,
    "mleti": F_BREWING,
    "typ mletia": F_BREWING,
    "hrubost kavy": F_BREWING,
    "hrubost mletia": F_BREWING,
    # sensory
    "chut": F_FLAVOR,
    "chute": F_FLAVOR,
    "chut a tony": F_FLAVOR,
    "chutovy profil": F_FLAVOR,
    "chutove tony": F_FLAVOR,
    "profil": F_FLAVOR,
    "tony": F_FLAVOR,
    "aroma": F_FLAVOR,
    "notes": F_FLAVOR,
    "tasting notes": F_FLAVOR,
    "taste profile": F_FLAVOR,
    "primary flavour note": F_FLAVOR,
    "our baristas notes": F_FLAVOR,
    "telo": F_BODY,
    "body": F_BODY,
    "kyslost": F_ACIDITY,
    "kyselost": F_ACIDITY,
    "acidita": F_ACIDITY,
    "kyslost kavy": F_ACIDITY,
    "acidity": F_ACIDITY,
    "horkost": F_BITTERNESS,
    "horkost kavy": F_BITTERNESS,
    "bitterness": F_BITTERNESS,
    "sladkost": F_SWEETNESS,
    "sweetness": F_SWEETNESS,
    "sca": F_SCA,
    "sca skore": F_SCA,
    "skore": F_SCA,
    "score": F_SCA,
    "cupping": F_SCA,
    "cupping score": F_SCA,
    "cuppingove skore": F_SCA,
    "skore kvality": F_SCA,
    # composition and packaging
    "druh": F_SPECIES,
    "druh kavy": F_SPECIES,
    "typ kavy": F_SPECIES,
    "zlozenie": F_SPECIES,
    "slozeni": F_SPECIES,
    "zlozenie kavy": F_SPECIES,
    "zlozenie podla druhu": F_SPECIES,
    "arabica": F_SPECIES,
    "arabika": F_SPECIES,
    "robusta": F_SPECIES,
    "pomer": F_SPECIES,
    "species": F_SPECIES,
    "bezkofeinova": F_DECAF,
    "decaf": F_DECAF,
    "obsah kofeinu": F_DECAF,
    # A WooCommerce weight attribute is the bag of beans, not the parcel.
    "hmotnost": F_WEIGHT,
    "hmotnost produktu": F_WEIGHT,
    "vaha": F_WEIGHT,
    "vaha balenia": F_WEIGHT,
    "gramaz": F_WEIGHT,
    "velkost balenia": F_WEIGHT,
    "velikost baleni": F_WEIGHT,
    "velkost": F_WEIGHT,
    "balenie": F_WEIGHT,
    "baleni": F_WEIGHT,
    "typ balenia": F_WEIGHT,
    "typ baleni": F_WEIGHT,
    "obsah balenia": F_WEIGHT,
    "obal": F_WEIGHT,
    "weight": F_WEIGHT,
    "package weight": F_WEIGHT,
    "package size": F_WEIGHT,
    "net weight": F_WEIGHT,
    "certifikacia": F_CERTIFICATIONS,
    "certifikace": F_CERTIFICATIONS,
    "certifikat": F_CERTIFICATIONS,
    "certification": F_CERTIFICATIONS,
}


class WooConfigError(ValueError):
    """Raised when a shop TOML is missing a key ``WooSite`` cannot invent."""

    def __init__(self, path: Path, problem: str) -> None:
        """Build the error.

        Args:
            path: The configuration file that is wrong.
            problem: What is wrong with it.
        """
        super().__init__(f"{path}: {problem}")
        self.path = path


class StoreApiUnavailableError(RuntimeError):
    """Raised inside discovery when the Store API cannot be read at all.

    It never leaves the adapter: :meth:`WooSite.discover` catches it and walks
    the shop's HTML listings instead, which is what makes a shop whose
    ``/wp-json/`` is blocked (``ebenica.sk``) or unplugged still crawlable.
    """

    def __init__(self, url: str, reason: str) -> None:
        """Build the error.

        Args:
            url: The Store API URL that could not be read.
            reason: Why it could not be read.
        """
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason


@dataclass(slots=True)
class WooConfig:
    """Everything that differs between two WooCommerce shops.

    Attributes:
        site_id: Registry id, e.g. ``"kavaloka"``.
        name: Human-readable shop name.
        country: Which market the shop sells in.
        base_url: Shop root, used to build the Store API URLs.
        platform: Always ``"woocommerce"``; kept so the mapping round-trips.
        mode: ``"api"`` reads the Store API and falls back to HTML when it
            cannot; ``"html"`` never touches ``/wp-json/`` at all.
        currency: Fallback currency when a payload states none.
        api_category_ids: Product category ids to pull, most specific first.
        api_category_slugs: Slugs resolved against the categories endpoint when
            no ids are pinned; ids are stable, slugs are not.
        category_urls: The HTML listing pages, used in HTML mode and as the
            fallback for a shop whose Store API turns out to be unreadable.
        label_map: Folded attribute name -> field, merged over
            :data:`DEFAULT_LABEL_MAP`.
        ignore: Extra folded markers for :meth:`WooSite.is_ignored`.
        max_pages: Hard cap on listing pages per category.
        per_page: Products per Store API page, capped at the API's own 100.
    """

    site_id: str
    name: str
    country: Literal["CZ", "SK"]
    base_url: str
    platform: str = PLATFORM
    mode: Literal["api", "html"] = "api"
    currency: str | None = None
    api_category_ids: list[int] = field(default_factory=list)
    api_category_slugs: list[str] = field(default_factory=list)
    category_urls: list[str] = field(default_factory=list)
    label_map: dict[str, str] = field(default_factory=dict)
    ignore: list[str] = field(default_factory=list)
    max_pages: int = DEFAULT_MAX_PAGES
    per_page: int = DEFAULT_PER_PAGE

    @classmethod
    def from_mapping(cls, config: dict[str, Any], path: Path) -> WooConfig:
        """Validate one parsed TOML mapping into a config.

        Args:
            config: The mapping ``tomllib`` produced.
            path: Where it came from, for error messages.

        Returns:
            The validated configuration.

        Raises:
            WooConfigError: When a required key is missing or malformed.
        """
        missing = [key for key in ("site_id", "name", "country", "base_url") if not config.get(key)]
        if missing:
            raise WooConfigError(path, f"missing required key(s): {', '.join(missing)}")
        country = str(config["country"]).upper()
        if country not in {"CZ", "SK"}:
            raise WooConfigError(path, f"country must be CZ or SK, not {country!r}")
        mode = str(config.get("mode", "api")).lower()
        if mode not in {"api", "html"}:
            raise WooConfigError(path, f"mode must be api or html, not {mode!r}")
        base_url = str(config["base_url"])
        base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        categories = [str(url) for url in config.get("category_urls", []) if str(url).strip()]
        if mode == "html" and not categories:
            raise WooConfigError(
                path, "category_urls must list at least one listing URL in html mode"
            )
        label_map = {
            normalize.fold(key): str(value) for key, value in config.get("label_map", {}).items()
        }
        _check_label_map(label_map, config.get("label_map", {}), path)
        return cls(
            site_id=str(config["site_id"]),
            name=str(config["name"]),
            country=cast("Literal['CZ', 'SK']", country),
            base_url=base_url,
            mode=cast("Literal['api', 'html']", mode),
            currency=str(config["currency"]) if config.get("currency") else None,
            api_category_ids=_category_ids(config, path),
            api_category_slugs=[str(slug) for slug in config.get("api_category_slugs", [])],
            category_urls=[urljoin(base_url, url) for url in categories],
            label_map=label_map,
            ignore=[normalize.fold(marker) for marker in config.get("ignore", [])],
            max_pages=int(config.get("max_pages", DEFAULT_MAX_PAGES)),
            per_page=min(int(config.get("per_page", DEFAULT_PER_PAGE)), MAX_PER_PAGE),
        )


def _category_ids(config: dict[str, Any], path: Path) -> list[int]:
    """Read and validate the pinned Store API category ids.

    Args:
        config: The parsed TOML mapping.
        path: Where it came from, for error messages.

    Returns:
        The ids, in the order the file lists them.

    Raises:
        WooConfigError: When an entry is not a whole number.
    """
    ids: list[int] = []
    for value in config.get("api_category_ids", []):
        try:
            ids.append(int(value))
        except (TypeError, ValueError) as exc:
            raise WooConfigError(path, f"api_category_ids must be integers, not {value!r}") from exc
    return ids


def _check_label_map(folded: dict[str, str], written: dict[str, Any], path: Path) -> None:
    """Refuse a ``label_map`` that points a label at a field nobody reads.

    Args:
        folded: The folded label -> field mapping the TOML asked for.
        written: The same mapping with the labels as the file spells them.
        path: The configuration file, for the error message.

    Raises:
        WooConfigError: When a value is not one of Shoptet's ``KNOWN_FIELDS``,
            which both platforms share so one sink schema serves both.
    """
    spellings = {normalize.fold(label): str(label) for label in written}
    for label, field_name in folded.items():
        if field_name in KNOWN_FIELDS:
            continue
        valid = ", ".join(sorted(name for name in KNOWN_FIELDS if name))
        raise WooConfigError(
            path,
            f"label_map[{spellings.get(label, label)!r}] = {field_name!r} is not a field; "
            f"valid fields are: {valid}",
        )


def build(config: dict[str, Any], path: Path) -> SiteAdapter:
    """Build one configured WooCommerce shop.

    Args:
        config: The parsed TOML mapping.
        path: Where it came from, for error messages.

    Returns:
        The ready-to-use adapter.
    """
    return WooSite(WooConfig.from_mapping(config, path))


# --- Store API helpers -------------------------------------------------------


def minor_amount(raw: object, minor_unit: int) -> float | None:
    """Turn a Store API price string into an amount of the shop's currency.

    The Store API quotes money in *minor units* with the exponent alongside it,
    and the exponent is not always two: ``kmen.coffee`` and ``kavaloka.cz``
    publish whole crowns with ``currency_minor_unit = 0``, so a hard-coded
    ``/100`` would price their coffee at a hundredth of its real cost.

    Args:
        raw: The ``prices`` string, e.g. ``"28700"``.
        minor_unit: ``prices.currency_minor_unit``.

    Returns:
        The amount, or None when the shop states none — ``"0"`` is WooCommerce
        for "price on request", never a free bag of coffee.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    exponent = min(max(minor_unit, 0), _MAX_MINOR_UNIT)
    try:
        value = int(text)
    except ValueError:
        return normalize.parse_amount(text)
    if value == 0:
        return None
    divisor = float(10**exponent)
    return round(value / divisor, exponent)


def _items(payload: str, url: str) -> list[dict[str, Any]]:
    """Read one Store API products page.

    Args:
        payload: The response body.
        url: The URL it came from, for the error message.

    Returns:
        The product objects the page lists.

    Raises:
        StoreApiUnavailableError: When the body is not a JSON array of objects,
            which is what a WAF challenge, a login wall or a plain HTML 404
            page looks like from here.
    """
    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        raise StoreApiUnavailableError(url, f"not JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise StoreApiUnavailableError(url, f"expected a JSON array, got {type(parsed).__name__}")
    return [item for item in parsed if isinstance(item, dict)]


def _text_of(markup: str | None) -> str | None:
    """Strip the tags off one of the API's HTML fields.

    Args:
        markup: The ``description`` or ``short_description`` value.

    Returns:
        The visible text, or None when the field is empty.
    """
    if not markup:
        return None
    return dom.text(BeautifulSoup(markup, "lxml"))


def _blocks(item: dict[str, Any]) -> list[Tag]:
    """Parse the product's two description fields into DOM blocks.

    Both are read: four shops in the survey leave ``description`` empty and
    seven leave ``short_description`` empty, and either one may be the only
    place a ``Label: value`` block exists.

    Args:
        item: One Store API product object.

    Returns:
        One soup per non-empty field, short description first.

    """
    markup = [item.get("short_description"), item.get("description")]
    return [BeautifulSoup(str(text), "lxml") for text in markup if text]


def _table_rows(blocks: Iterable[Tag]) -> Iterator[tuple[str | None, str | None]]:
    """Yield the label/value pairs of any table inside a description.

    ``severan.eu`` ships its coffee facts as a ``<table class="coffee-table">``
    in ``short_description`` rather than as attributes or label lines, and a
    table is cheap enough to read everywhere.

    Args:
        blocks: The parsed description blocks.

    Yields:
        One ``(label, value)`` pair per two-cell row.
    """
    for block in blocks:
        for row in block.select("tr"):
            cells = row.select("th, td")
            if len(cells) >= 2:  # noqa: PLR2004  (a label and its value)
                yield dom.text(cells[0]), dom.text(cells[1])


def _terms(attribute: dict[str, Any]) -> list[str]:
    """Return one attribute's term names, unescaped.

    Args:
        attribute: One entry of the product's ``attributes`` array.

    Returns:
        The names in API order.
    """
    terms = attribute.get("terms")
    if not isinstance(terms, list):
        return []
    return dom.unique(
        unescape(str(term.get("name")))
        for term in terms
        if isinstance(term, dict) and term.get("name")
    )


def _term_names(item: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Map ``(attribute name, term slug)`` to the term's human label.

    A ``variations`` entry names its terms by slug (``"100-g"``), while the
    readable weight only exists on the attribute's term list (``"100 g"``).

    Args:
        item: One Store API product object.

    Returns:
        The lookup table.
    """
    table: dict[tuple[str, str], str] = {}
    for attribute in item.get("attributes", []):
        if not isinstance(attribute, dict):
            continue
        name = unescape(str(attribute.get("name") or ""))
        for term in attribute.get("terms") or []:
            if isinstance(term, dict) and term.get("slug"):
                table[name, str(term["slug"])] = unescape(str(term.get("name") or term["slug"]))
    return table


def _variant_weight(labels: list[str]) -> int | None:
    """Pick the weight out of one variation's term labels.

    Args:
        labels: The variation's term labels, one per axis.

    Returns:
        The weight in grams, or None when no axis states one.
    """
    for label in labels:
        grams = normalize.parse_weight_grams(label)
        if grams is not None:
            return grams
    return None


def _api_variants(item: dict[str, Any], ref: ProductRef, currency: str | None) -> list[Variant]:
    """Build one variant per purchasable variation the API lists.

    The Store API's ``variations`` array carries ids and attribute terms but no
    per-variant price — ``prices.price`` is the *cheapest* variant and
    ``price_range`` only spans the extremes — so a variant's price is left
    unknown unless the product has exactly one variation, in which case the
    headline price is unambiguously its own.

    Args:
        item: One Store API product object.
        ref: The product reference being parsed.
        currency: The product's currency.

    Returns:
        The variants, or an empty list for a simple product.
    """
    variations = [entry for entry in item.get("variations") or [] if isinstance(entry, dict)]
    if not variations:
        return []
    names = _term_names(item)
    prices = item.get("prices") or {}
    minor_unit = int(prices.get("currency_minor_unit", 2) or 0)
    only = minor_amount(prices.get("price"), minor_unit) if len(variations) == 1 else None
    variants: list[Variant] = []
    for entry in variations:
        labels = [
            names.get(
                (unescape(str(axis.get("name") or "")), str(axis.get("value"))),
                str(axis.get("value")),
            )
            for axis in entry.get("attributes") or []
            if isinstance(axis, dict) and axis.get("value")
        ]
        variants.append(
            Variant(
                external_id=str(entry.get("id")) if entry.get("id") else None,
                url=ref.url,
                weight_g=_variant_weight(labels),
                price=only,
                currency=currency if only is not None else None,
                label=" / ".join(labels) or None,
            )
        )
    return variants


def _headline_weight(labels: _Labels, name: str, variants: list[Variant]) -> int | None:
    """Work out the weight the headline price refers to.

    ``prices.price`` is the cheapest variant of a variable product, so the
    weight that goes with it is the *smallest* one on offer — not the first the
    weight attribute happens to list, which for ``ripit.sk`` is ``"1000 g"``
    against a price for 250 g.

    Args:
        labels: Every labelled value the payload states.
        name: The product name, which often ends in ``250g``.
        variants: The parsed variants.

    Returns:
        The weight in grams, or None.
    """
    weights = [variant.weight_g for variant in variants if variant.weight_g is not None]
    if weights:
        return min(weights)
    from_label = normalize.parse_weight_grams(labels.get(F_WEIGHT))
    if from_label is not None:
        return from_label
    return normalize.parse_weight_grams(name)


def _popularity(item: dict[str, Any]) -> Popularity:
    """Read the shop-side social proof the payload carries.

    Args:
        item: One Store API product object.

    Returns:
        The social-proof part of the model.
    """
    rating = normalize.parse_float(str(item.get("average_rating") or ""))
    return Popularity(
        rating=rating or None,
        review_count=normalize.parse_int(str(item.get("review_count") or "")),
    )


def _names(entries: object) -> list[str]:
    """Return the ``name`` of every object in one of the API's little arrays.

    Args:
        entries: The ``categories`` or ``tags`` value.

    Returns:
        The names, unescaped and de-duplicated.
    """
    if not isinstance(entries, list):
        return []
    return dom.unique(
        unescape(str(entry.get("name")))
        for entry in entries
        if isinstance(entry, dict) and entry.get("name")
    )


def _images(item: dict[str, Any]) -> list[str]:
    """Collect the product photos.

    Args:
        item: One Store API product object.

    Returns:
        Full-size image URLs, de-duplicated, in API order.
    """
    entries = item.get("images")
    if not isinstance(entries, list):
        return []
    return dom.unique(
        str(entry.get("src")) for entry in entries if isinstance(entry, dict) and entry.get("src")
    )


# --- HTML helpers ------------------------------------------------------------

#: A WooCommerce product card, whatever the theme calls its grid.
_CARD_SELECTOR: Final = "li.product, div.product.type-product"
#: A stripped gallery placeholder, and any other inline payload masquerading as
#: an image URL.
_DATA_URI: Final = "data:"


def _post_id(card: Tag) -> str | None:
    """Read the WordPress post id off a product card's class list.

    Args:
        card: The ``li.product`` element.

    Returns:
        The id, or None when the theme states none.
    """
    for token in dom.classes(card):
        if token.startswith("post-") and token[5:].isdigit():
            return token[5:]
    return dom.attr(card.select_one("[data-product_id]"), "data-product_id")


def _image_src(tag: Tag | None) -> str | None:
    """Read a real image URL off an ``<img>``, skipping lazy-load placeholders.

    Args:
        tag: The image element, or None.

    Returns:
        The URL, or None when the element only holds a placeholder.
    """
    for name in ("data-src", "data-lazy-src", "src"):
        value = dom.attr(tag, name)
        if value and not value.startswith(_DATA_URI):
            return value
    return None


def _json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Return every JSON-LD object on the page, ``@graph`` members included.

    Args:
        soup: The whole parsed page.

    Returns:
        The objects, in document order.
    """
    found: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            parsed = json.loads(script.string or "{}")
        except ValueError:
            continue
        for entry in parsed if isinstance(parsed, list) else [parsed]:
            if not isinstance(entry, dict):
                continue
            graph = entry.get("@graph")
            found.extend(node for node in graph if isinstance(node, dict)) if isinstance(
                graph, list
            ) else found.append(entry)
    return found


def _ld_product(soup: BeautifulSoup) -> dict[str, Any] | None:
    """Return the page's own ``schema.org/Product`` node.

    WooCommerce themes emit one node per related product too, so the first one
    that actually carries an offer is taken — that is the page's own.

    Args:
        soup: The whole parsed page.

    Returns:
        The node, or None when the theme publishes no JSON-LD.
    """
    products = [node for node in _json_ld(soup) if node.get("@type") == "Product"]
    for node in products:
        if node.get("offers"):
            return node
    return products[0] if products else None


def _ld_offer(node: dict[str, Any] | None) -> dict[str, Any]:
    """Return the first offer of a JSON-LD product node.

    Args:
        node: The product node, or None.

    Returns:
        The offer object, or an empty mapping.
    """
    offers = (node or {}).get("offers")
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list):
        return next((offer for offer in offers if isinstance(offer, dict)), {})
    return {}


def _html_variants(root: Tag, ref: ProductRef, currency: str | None) -> list[Variant]:
    """Build variants from the variation form's ``data-product_variations``.

    This is the one place a WooCommerce shop states a price *per weight*: the
    Store API never does, so the detail page is worth parsing even for shops
    whose API works.

    Args:
        root: The page, or the product wrapper.
        ref: The product reference being parsed.
        currency: The product's currency.

    Returns:
        The variants, or an empty list for a simple product.
    """
    form = root.select_one("form.variations_form[data-product_variations]")
    raw = dom.attr(form, "data-product_variations")
    if not raw:
        return []
    try:
        parsed = json.loads(unescape(raw))
    except ValueError:
        logger.debug("unreadable data-product_variations on %s", ref.url)
        return []
    if not isinstance(parsed, list):
        return []
    variants: list[Variant] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        labels = [str(value) for value in (entry.get("attributes") or {}).values() if value]
        variants.append(
            Variant(
                external_id=str(entry.get("variation_id")) if entry.get("variation_id") else None,
                url=ref.url,
                weight_g=_variant_weight(labels),
                price=normalize.parse_float(str(entry.get("display_price"))),
                currency=currency,
                available=bool(entry["is_in_stock"]) if "is_in_stock" in entry else None,
                label=" / ".join(labels) or None,
            )
        )
    return variants


class WooSite(SiteAdapter):
    """One WooCommerce shop, parameterised entirely by its :class:`WooConfig`.

    Discovery is API-first: the Store API answers on 38 of the 39 Czech and
    Slovak roasteries surveyed, needs no authentication, states stock and
    machine-readable prices, and hands back the whole product in the listing
    response — so a crawl costs one request per hundred products and never
    fetches a detail page at all.
    """

    kind = PLATFORM

    def __init__(self, config: WooConfig) -> None:
        """Build the adapter.

        Args:
            config: The shop's validated configuration.
        """
        self.config = config
        self.site_id = config.site_id
        self.name = config.name
        self.country = config.country
        self.base_url = config.base_url
        self.max_pages = config.max_pages
        self.label_map = {**DEFAULT_LABEL_MAP, **config.label_map}

    def ignored_names(self) -> tuple[str, ...]:
        """Return the folded markers of products that are not coffee beans.

        Returns:
            The shared defaults plus whatever the shop's TOML added.
        """
        return (*DEFAULT_IGNORED, *self.config.ignore)

    # --- discovery -----------------------------------------------------------

    def api_url(self, page: int, category: int | None = None) -> str:
        """Return the Store API URL of one page of products.

        Args:
            page: The 1-based page number.
            category: The product category id to filter on, if any.

        Returns:
            The absolute URL.
        """
        query: dict[str, str | int] = {"per_page": self.config.per_page, "page": page}
        if category is not None:
            query["category"] = category
        return f"{urljoin(self.base_url, STORE_PRODUCTS_PATH)}?{urlencode(query)}"

    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        """Walk the shop's catalogue and yield one reference per product.

        Args:
            fetcher: The shared polite fetcher.
            max_pages: Cap on listing pages per category, for this run only.

        Yields:
            One reference per product found; API references carry the product's
            own JSON in ``payload``, so the pipeline never fetches their URL.
        """
        cap = max_pages if max_pages is not None else self.max_pages
        if self.config.mode == "api":
            try:
                yield from self._discover_api(fetcher, cap)
            except StoreApiUnavailableError as exc:
                logger.warning(
                    "%s: Store API unavailable (%s); falling back to HTML listings",
                    self.site_id,
                    exc,
                )
            else:
                return
        yield from self._discover_html(fetcher, cap)

    def _discover_api(self, fetcher: PoliteFetcher, cap: int) -> Iterator[ProductRef]:
        """Page through the Store API, one walk per configured category.

        ``X-WP-TotalPages`` is not consulted: :class:`~coffee_aggregator.http.
        FetchResult` exposes no headers, and the header lies anyway on at least
        one surveyed shop, so the walk stops on the first empty page.

        Args:
            fetcher: The shared polite fetcher.
            cap: How many pages of each category may be walked.

        Yields:
            One reference per product not yielded yet.

        Raises:
            StoreApiUnavailableError: When the API failed before a single
                product came back, so the caller may still fall back to HTML.
        """
        seen: set[str] = set()
        for category in self._categories(fetcher):
            for page in range(1, cap + 1):
                url = self.api_url(page, category)
                try:
                    items = _items(self._fetch(fetcher, url), url)
                except StoreApiUnavailableError:
                    if not seen:
                        raise
                    logger.warning("%s: Store API stopped answering at %s", self.site_id, url)
                    return
                if not items:
                    logger.debug("%s: %s lists no products, stopping", self.site_id, url)
                    break
                for item in items:
                    ref = self._api_ref(item)
                    if ref is None or ref.external_id in seen:
                        continue
                    seen.add(ref.external_id)
                    yield ref

    def _fetch(self, fetcher: PoliteFetcher, url: str) -> str:
        """Retrieve one Store API URL, turning every refusal into one error.

        robots.txt is honoured for the API exactly as for a page — the fetcher
        gates every URL — so a shop that disallows ``/wp-json/`` ends up on the
        HTML path instead of being crawled against its wishes.

        Args:
            fetcher: The shared polite fetcher.
            url: The absolute Store API URL.

        Returns:
            The response body.

        Raises:
            StoreApiUnavailableError: When the URL could not be read.
        """
        try:
            return fetcher.get(url).text
        except Exception as exc:
            raise StoreApiUnavailableError(url, f"{type(exc).__name__}: {exc}") from exc

    def _categories(self, fetcher: PoliteFetcher) -> list[int | None]:
        """Decide which product categories the API walk should filter on.

        Args:
            fetcher: The shared polite fetcher.

        Returns:
            The category ids, or ``[None]`` to take the whole catalogue —
            which is right only for the handful of shops that sell nothing but
            coffee, since ``X-WP-Total`` counts gear and gift cards too.

        Raises:
            StoreApiUnavailableError: When slugs must be resolved and the
                categories endpoint cannot be read.
        """
        if self.config.api_category_ids:
            return list(self.config.api_category_ids)
        if not self.config.api_category_slugs:
            return [None]
        url = f"{urljoin(self.base_url, STORE_CATEGORIES_PATH)}?{urlencode({'per_page': 100})}"
        wanted = {normalize.fold(slug) for slug in self.config.api_category_slugs}
        resolved = [
            int(entry["id"])
            for entry in _items(self._fetch(fetcher, url), url)
            if normalize.fold(str(entry.get("slug"))) in wanted
            and str(entry.get("id", "")).isdigit()
            # A plugin injects 16-digit synthetic terms beside the real ones.
            and int(entry["id"]) < _MAX_REAL_CATEGORY_ID
        ]
        if not resolved:
            logger.warning("%s: no category matched %s", self.site_id, sorted(wanted))
            return [None]
        return cast("list[int | None]", resolved)

    def _api_ref(self, item: dict[str, Any]) -> ProductRef | None:
        """Turn one Store API product into a reference carrying its own JSON.

        Args:
            item: One Store API product object.

        Returns:
            The reference, or None when the item is not coffee beans or states
            no URL at all.
        """
        url = str(item.get("permalink") or "")
        if not url:
            logger.debug("%s: a Store API product has no permalink", self.site_id)
            return None
        name = unescape(str(item.get("name") or ""))
        if self.is_ignored(name):
            logger.debug("%s: %s is not coffee beans, skipping", self.site_id, name)
            return None
        prices = item.get("prices") or {}
        minor_unit = int(prices.get("currency_minor_unit", 2) or 0)
        price = minor_amount(prices.get("price"), minor_unit)
        images = _images(item)
        return ProductRef(
            site_id=self.site_id,
            external_id=str(item.get("id") or url.rstrip("/").rsplit("/", 1)[-1]),
            url=url,
            name=name or None,
            price=price,
            currency=(prices.get("currency_code") or self.config.currency) if price else None,
            image_url=images[0] if images else None,
            payload=json.dumps(item, ensure_ascii=False),
        )

    def parse_listing(self, html_text: str) -> list[ProductRef]:
        """Turn one HTML listing page into product references.

        Args:
            html_text: The listing page source.

        Returns:
            One reference per product card, in page order.
        """
        soup = BeautifulSoup(html_text, "lxml")
        grid = soup.select_one("ul.products")
        scope: Tag | BeautifulSoup = grid if grid is not None else soup
        refs = [self._listing_ref(card) for card in scope.select(_CARD_SELECTOR)]
        return [ref for ref in refs if ref is not None]

    def _listing_ref(self, card: Tag) -> ProductRef | None:
        """Turn one product card into a reference.

        Args:
            card: The card element.

        Returns:
            The reference, or None when the card has no usable link.
        """
        link = card.select_one("a.woocommerce-LoopProduct-link, a[href]")
        url = dom.absolute(self.base_url, dom.attr(link, "href"))
        if url is None:
            logger.debug("skipping a %s product card without a link", self.site_id)
            return None
        price, currency = normalize.parse_price(
            dom.text(card.select_one(".price bdi, .price .amount, .price"))
        )
        return ProductRef(
            site_id=self.site_id,
            external_id=_post_id(card) or url.rstrip("/").rsplit("/", 1)[-1],
            url=url,
            name=dom.text(card.select_one(".woocommerce-loop-product__title, h2, h3")),
            price=price,
            currency=(currency or self.config.currency) if price is not None else None,
            image_url=dom.absolute(self.base_url, _image_src(card.select_one("img"))),
        )

    def next_page_url(self, html_text: str, current_url: str, page: int) -> str | None:
        """Return the URL of the listing page after this one.

        The rendered ``a.next`` link is preferred over ``/page/N/`` because the
        permalink base is localised — ``ebenica.sk`` paginates on ``/strana/2/``.

        Args:
            html_text: The current listing page source.
            current_url: The URL that page came from.
            page: The 1-based number of the current page.

        Returns:
            The next URL, or None when this was the last page.
        """
        soup = BeautifulSoup(html_text, "lxml")
        following = dom.absolute(
            current_url, dom.attr(soup.select_one("a.next, .woocommerce-pagination a.next"), "href")
        )
        if following and following != current_url:
            return following
        if soup.select_one(".woocommerce-pagination, .pagination") is None:
            return None
        base = current_url if current_url.endswith("/") else f"{current_url}/"
        return f"{base}page/{page + 1}/"

    def _discover_html(self, fetcher: PoliteFetcher, cap: int) -> Iterator[ProductRef]:
        """Walk the shop's HTML category listings.

        Args:
            fetcher: The shared polite fetcher.
            cap: How many pages of each category may be walked.

        Yields:
            One reference per product not yielded yet.
        """
        if not self.config.category_urls:
            logger.warning("%s: no category_urls to fall back on", self.site_id)
            return
        seen: set[str] = set()
        for category_url in self.config.category_urls:
            url: str | None = category_url
            previous: set[str] | None = None
            for page in range(1, cap + 1):
                if url is None:
                    break
                body = fetcher.get(url).text
                refs = self.parse_listing(body)
                if not refs:
                    logger.debug("%s: %s lists no products, stopping", self.site_id, url)
                    break
                ids = {ref.external_id for ref in refs}
                if ids == previous:
                    logger.debug("%s: %s repeats the previous page, stopping", self.site_id, url)
                    break
                previous = ids
                fresh = [ref for ref in refs if ref.external_id not in seen]
                seen.update(ids)
                yield from fresh
                url = self.next_page_url(body, url, page)

    # --- parsing -------------------------------------------------------------

    def parse_product(self, html: str, ref: ProductRef) -> Coffee | None:
        """Turn one product's source into a coffee.

        Args:
            html: The Store API payload when discovery carried one, otherwise
                the detail page source.
            ref: What discovery already told us about this product.

        Returns:
            The parsed coffee, or None for cascara, merchandise and tasting
            packs — and for a payload that turns out to be unreadable.
        """
        if ref.payload is not None:
            return self._parse_api(ref.payload, ref)
        return self._parse_html(html, ref)

    def _labels(self, item: dict[str, Any]) -> tuple[_Labels, list[str], str | None]:
        """Read every labelled value a Store API product states.

        Three tiers, in descending reliability: the ``attributes`` array, the
        ``Label: value`` lines of both description fields, and any table inside
        them. Together they cover 33 of the 38 surveyed shops.

        Args:
            item: One Store API product object.

        Returns:
            The labels, the description prose, and the short description.
        """
        labels = _Labels()
        for attribute in item.get("attributes", []):
            if isinstance(attribute, dict):
                labels.add(
                    unescape(str(attribute.get("name") or "")),
                    ", ".join(_terms(attribute)),
                    self.label_map,
                )
        blocks = _blocks(item)
        for label, value in _table_rows(blocks):
            labels.add(label, value, self.label_map)
        parsed = dom.parse_label_lines(blocks)
        for label, value in parsed.pairs:
            labels.add(label, value, self.label_map)
        return labels, parsed.prose, _text_of(item.get("short_description"))

    def _parse_api(self, payload: str, ref: ProductRef) -> Coffee | None:
        """Turn one Store API product into a coffee.

        Args:
            payload: The product's own JSON, as discovery captured it.
            ref: The product reference being parsed.

        Returns:
            The parsed coffee, or None when the payload is unreadable or the
            product is not coffee beans.
        """
        try:
            item = json.loads(payload)
        except ValueError:
            logger.warning("%s: unreadable payload for %s", self.site_id, ref.url)
            return None
        if not isinstance(item, dict):
            return None
        name = unescape(str(item.get("name") or ref.name or ""))
        if self.is_ignored(name):
            logger.debug("%s: %s is not coffee beans, skipping", self.site_id, name)
            return None
        labels, prose, summary = self._labels(item)
        prices = item.get("prices") or {}
        minor_unit = int(prices.get("currency_minor_unit", 2) or 0)
        price = minor_amount(prices.get("price"), minor_unit)
        currency = str(prices.get("currency_code") or "") or self.config.currency
        categories = _names(item.get("categories"))
        species = _parse_species(labels, name)
        variants = _api_variants(item, ref, currency)
        if item.get("sku"):
            labels.raw.setdefault("SKU", str(item["sku"]))
        return Coffee(
            site=self.site_id,
            external_id=str(item.get("id") or ref.external_id),
            url=str(item.get("permalink") or ref.url),
            name=name,
            site_country=self.country,
            price=price,
            currency=currency if price is not None else None,
            weight_g=_headline_weight(labels, name, variants),
            available=bool(item["is_in_stock"]) if "is_in_stock" in item else None,
            decaf=_is_decaf(labels, name, categories),
            origin=_parse_origin(labels, name, blend=species.is_blend),
            processing=normalize.parse_processing(labels.get(F_PROCESS)),
            roast=_parse_roast(labels, categories),
            species=species,
            taste=_parse_taste(labels, summary),
            popularity=_popularity(item),
            variants=variants,
            images=_images(item),
            tags=_names(item.get("tags")),
            categories=categories,
            certifications=_option_list(labels.get(F_CERTIFICATIONS)),
            specialty_grade=_specialty_grade(name, categories, _score(labels)),
            original_price=_original_price(prices, minor_unit, price),
            description="\n".join(prose) or summary,
            origin_text=labels.get(F_COUNTRY),
            raw_attributes=labels.raw,
        )

    def _parse_html(self, html_text: str, ref: ProductRef) -> Coffee | None:
        """Turn one WooCommerce detail page into a coffee.

        Args:
            html_text: The detail page source.
            ref: The product reference being parsed.

        Returns:
            The parsed coffee, or None when the page is not a product page or
            the product is not coffee beans.
        """
        soup = BeautifulSoup(html_text, "lxml")
        root = soup.select_one("div.product.type-product, .summary") or soup
        node = _ld_product(soup)
        name = (
            dom.text(soup.select_one("h1.product_title"))
            or str((node or {}).get("name") or "")
            or dom.text(soup.select_one("h1"))
            or ref.name
            or ""
        )
        if not name or self.is_ignored(name):
            logger.debug("%s: %s is not coffee beans, skipping", self.site_id, name or ref.url)
            return None
        return self._build_html(soup, root, ref, name, node)

    def _build_html(
        self,
        soup: BeautifulSoup,
        root: Tag,
        ref: ProductRef,
        name: str,
        node: dict[str, Any] | None,
    ) -> Coffee:
        """Assemble the coffee once the detail page is known to be one.

        Args:
            soup: The whole page.
            root: The product wrapper.
            ref: The product reference being parsed.
            name: The product name.
            node: The page's JSON-LD product node, when it publishes one.

        Returns:
            The parsed coffee.
        """
        labels = _Labels()
        for label, value in _attribute_rows(root):
            labels.add(label, value, self.label_map)
        parsed = dom.parse_label_lines(_description_blocks(soup))
        for label, value in parsed.pairs:
            labels.add(label, value, self.label_map)
        for key, value in dom.page_meta(soup).items():
            labels.raw.setdefault(key, value)
        offer = _ld_offer(node)
        price, currency = self._html_price(root, offer, ref)
        variants = _html_variants(soup, ref, currency)
        categories = _categories_of(soup, root)
        species = _parse_species(labels, name)
        summary = dom.text(soup.select_one(".woocommerce-product-details__short-description"))
        return Coffee(
            site=self.site_id,
            external_id=str((node or {}).get("sku") or ref.external_id),
            url=ref.url,
            name=name,
            site_country=self.country,
            price=price,
            currency=currency if price is not None else None,
            weight_g=_headline_weight(labels, name, variants),
            available=_available(str(offer.get("availability") or "")),
            decaf=_is_decaf(labels, name, categories),
            origin=_parse_origin(labels, name, blend=species.is_blend),
            processing=normalize.parse_processing(labels.get(F_PROCESS)),
            roast=_parse_roast(labels, categories),
            species=species,
            taste=_parse_taste(labels, summary),
            popularity=Popularity(),
            variants=variants,
            images=_html_images(soup, node, self.base_url),
            tags=_tags_of(root),
            categories=categories,
            certifications=_option_list(labels.get(F_CERTIFICATIONS)),
            specialty_grade=_specialty_grade(name, categories, _score(labels)),
            description="\n".join(parsed.prose) or summary,
            origin_text=labels.get(F_COUNTRY),
            raw_attributes=labels.raw,
        )

    def _html_price(
        self,
        root: Tag,
        offer: dict[str, Any],
        ref: ProductRef,
    ) -> tuple[float | None, str | None]:
        """Read the detail page's headline price.

        Args:
            root: The product wrapper.
            offer: The JSON-LD offer, when the page publishes one.
            ref: The product reference being parsed.

        Returns:
            A ``(price, currency)`` tuple.
        """
        price = normalize.parse_amount(str(offer.get("price") or ""))
        currency = str(offer.get("priceCurrency") or "") or None
        if price is None:
            rendered = dom.text(root.select_one(".price bdi, .price .amount, .price"))
            price, detected = normalize.parse_price(rendered)
            currency = currency or detected
        return (
            price if price is not None else ref.price,
            currency or ref.currency or self.config.currency,
        )


def _original_price(prices: dict[str, Any], minor_unit: int, price: float | None) -> float | None:
    """Return the pre-discount price, when the product is actually discounted.

    Args:
        prices: The payload's ``prices`` object.
        minor_unit: ``prices.currency_minor_unit``.
        price: The price the product sells for now.

    Returns:
        The regular price, or None when it is not higher than the current one.
    """
    regular = minor_amount(prices.get("regular_price"), minor_unit)
    if regular is None or price is None or regular <= price:
        return None
    return regular


def _attribute_rows(root: Tag) -> Iterator[tuple[str | None, str | None]]:
    """Yield the rows of a theme's product-attribute table.

    Args:
        root: The product wrapper.

    Yields:
        One ``(label, value)`` pair per row.
    """
    for row in root.select("table.woocommerce-product-attributes tr, table.shop_attributes tr"):
        cells = row.select("th, td")
        if len(cells) >= 2:  # noqa: PLR2004  (a label and its value)
            yield dom.text(cells[0]), dom.text(cells[1])


def _description_blocks(soup: BeautifulSoup) -> list[Tag]:
    """Return the blocks a WooCommerce theme writes the product copy into.

    Args:
        soup: The whole parsed page.

    Returns:
        The blocks, short description first, in theme-preference order.
    """
    selectors = (
        ".woocommerce-product-details__short-description",
        "#tab-description",
        ".woocommerce-Tabs-panel--description",
        ".wc-tab.woocommerce-Tabs-panel",
    )
    return [block for selector in selectors for block in soup.select(selector)]


def _html_images(soup: BeautifulSoup, node: dict[str, Any] | None, base_url: str) -> list[str]:
    """Collect the product photos of a detail page.

    Args:
        soup: The whole parsed page.
        node: The JSON-LD product node, when the page publishes one.
        base_url: The shop root, for relative sources.

    Returns:
        Absolute image URLs, de-duplicated, in page order.
    """
    image = (node or {}).get("image")
    candidates = [str(image)] if isinstance(image, str) else []
    candidates.extend(
        src
        for tag in soup.select(".woocommerce-product-gallery img")
        if (src := _image_src(tag)) is not None
    )
    return dom.unique(dom.absolute(base_url, url) for url in candidates)


def _categories_of(soup: BeautifulSoup, root: Tag) -> list[str]:
    """Read the product's categories from the meta block or the breadcrumbs.

    Args:
        soup: The whole parsed page.
        root: The product wrapper.

    Returns:
        The category names, without the home link.
    """
    crumbs = [dom.text(tag) for tag in root.select(".posted_in a, .product_meta .posted_in a")]
    if not crumbs:
        crumbs = [dom.text(tag) for tag in soup.select(".woocommerce-breadcrumb a")][1:]
    return [
        value
        for value in dom.unique(crumbs)
        if normalize.fold(value) not in {"domu", "domov", "home", "uvod", "obchod", "shop"}
    ]


def _tags_of(root: Tag) -> list[str]:
    """Read the product's tags.

    Args:
        root: The product wrapper.

    Returns:
        The tag names, de-duplicated.
    """
    return dom.unique(
        dom.text(tag) for tag in root.select(".tagged_as a, .product_meta .tagged_as a")
    )
