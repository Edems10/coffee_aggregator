from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from itertools import product
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from coffee_aggregator import normalize
from coffee_aggregator.labels import (
    F_CERTIFICATIONS,
    F_COUNTRY,
    F_PROCESS,
    F_SHIP_WEIGHT,
    KNOWN_FIELDS,
    MAX_FUZZY_LABEL_WORDS,
    TERMS,
    Labels,
    build_map,
    headline_weight,
    is_decaf,
    map_label,
    option_list,
    parse_origin,
    parse_roast,
    parse_species,
    parse_taste,
    plausible_weight,
    read_lines,
    score,
    specialty_grade,
)
from coffee_aggregator.models import (
    DEFAULT_RATING_MAX,
    Coffee,
    Popularity,
    Variant,
)
from coffee_aggregator.sites import html as dom
from coffee_aggregator.sites.base import DEFAULT_IGNORED, ProductRef, SiteAdapter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from coffee_aggregator.http import PoliteFetcher

logger = logging.getLogger(__name__)

PLATFORM: Final = "shoptet"
DEFAULT_MAX_PAGES: Final = 50


#: ``raw_attributes`` key every roaster name is mirrored onto. The model has no
#: brand column yet, so a multi-brand retailer's "Pražiareň"/"Výrobca" row is
#: kept under both its own label and this one, and a typed column can read it.
BRAND_KEY: Final = "BRAND"
#: Folded labels that name who roasted the coffee.
_BRAND_LABELS: Final[frozenset[str]] = frozenset(
    {
        "praziaren",
        "prazirna",
        "vyrobca",
        "vyrobce",
        "znacka",
        "brand",
        "manufacturer",
    }
)
#: The ``"brand": "…"`` Shoptet writes into its dataLayer and JSON-LD blocks,
#: either as a plain string or as a ``{"@type": "Brand", "name": …}`` object.
_JSON_STRING: Final = r'"(?:[^"\\]|\\.){1,120}"'
_BRAND_JSON_RE: Final = re.compile(
    rf'"(?:brand|manufacturer)"\s*:\s*(?:({_JSON_STRING})'
    rf'|\{{[^{{}}]{{0,200}}?"name"\s*:\s*({_JSON_STRING}))',
    re.IGNORECASE,
)

# --- the fields a recognised label feeds -------------------------------------
# The vocabulary itself lives in coffee_aggregator.labels, shared with every
# other platform: a Czech or Slovak term any shop might print belongs there, so
# adding it once teaches every adapter at the same time.

#: Shoptet's own reading of two terms the shared vocabulary leaves out. In a
#: Shoptet parameter table "Hmotnost" is what the parcel weighs, not what is in
#: the bag; the bag weight comes from "Velikost balení" or from the name.
PLATFORM_TERMS: Final[dict[str, str]] = {
    "hmotnost": F_SHIP_WEIGHT,
    "weight": F_SHIP_WEIGHT,
}

DEFAULT_LABEL_MAP: Final[dict[str, str]] = build_map(TERMS, extra=PLATFORM_TERMS)


class ShoptetConfigError(ValueError):
    """Raised when a shop TOML is missing a key ``ShoptetSite`` cannot invent."""

    def __init__(self, path: Path, problem: str) -> None:
        """Build the error.

        Args:
            path: The configuration file that is wrong.
            problem: What is wrong with it.
        """
        super().__init__(f"{path}: {problem}")
        self.path = path


@dataclass(slots=True)
class ShoptetConfig:
    """Everything that differs between two Shoptet shops.

    Attributes:
        site_id: Registry id, e.g. ``"kavypitel"``.
        name: Human-readable shop name.
        country: Which market the shop sells in.
        base_url: Shop root, used to absolutise every href.
        category_urls: The listing pages to walk, most specific first.
        platform: Always ``"shoptet"``; kept so the mapping round-trips.
        currency: Fallback currency when a page states none.
        label_map: Folded parameter name -> field, merged over
            :data:`DEFAULT_LABEL_MAP`.
        ignore: Extra folded markers for :meth:`ShoptetSite.is_ignored`.
        max_pages: Hard cap on listing pages per category.
        pagination: ``"path"`` for ``/category/strana-2/`` (the Shoptet default)
            or ``"query"`` for ``/category/?page=2``.
    """

    site_id: str
    name: str
    country: Literal["CZ", "SK"]
    base_url: str
    category_urls: list[str]
    platform: str = PLATFORM
    currency: str | None = None
    label_map: dict[str, str] = field(default_factory=dict)
    ignore: list[str] = field(default_factory=list)
    max_pages: int = DEFAULT_MAX_PAGES
    pagination: Literal["path", "query"] = "path"

    @classmethod
    def from_mapping(cls, config: dict[str, Any], path: Path) -> ShoptetConfig:
        """Validate one parsed TOML mapping into a config.

        Args:
            config: The mapping ``tomllib`` produced.
            path: Where it came from, for error messages.

        Returns:
            The validated configuration.

        Raises:
            ShoptetConfigError: When a required key is missing or malformed.
        """
        missing = [key for key in ("site_id", "name", "country", "base_url") if not config.get(key)]
        if missing:
            raise ShoptetConfigError(path, f"missing required key(s): {', '.join(missing)}")
        categories = [str(url) for url in config.get("category_urls", []) if str(url).strip()]
        if not categories:
            raise ShoptetConfigError(path, "category_urls must list at least one listing URL")
        country = str(config["country"]).upper()
        if country not in {"CZ", "SK"}:
            raise ShoptetConfigError(path, f"country must be CZ or SK, not {country!r}")
        pagination = str(config.get("pagination", "path")).lower()
        if pagination not in {"path", "query"}:
            raise ShoptetConfigError(path, f"pagination must be path or query, not {pagination!r}")
        base_url = str(config["base_url"])
        label_map = {
            normalize.fold(key): str(value) for key, value in config.get("label_map", {}).items()
        }
        _check_label_map(label_map, config.get("label_map", {}), path)
        return cls(
            site_id=str(config["site_id"]),
            name=str(config["name"]),
            country=cast("Literal['CZ', 'SK']", country),
            base_url=base_url if base_url.endswith("/") else f"{base_url}/",
            category_urls=[urljoin(base_url, url) for url in categories],
            currency=str(config["currency"]) if config.get("currency") else None,
            label_map=label_map,
            ignore=[normalize.fold(marker) for marker in config.get("ignore", [])],
            max_pages=int(config.get("max_pages", DEFAULT_MAX_PAGES)),
            pagination=cast("Literal['path', 'query']", pagination),
        )


def _check_label_map(folded: dict[str, str], written: dict[str, Any], path: Path) -> None:
    """Refuse a ``label_map`` that points a label at a field nobody reads.

    Args:
        folded: The folded label -> field mapping the TOML asked for.
        written: The same mapping with the labels as the file spells them.
        path: The configuration file, for the error message.

    Raises:
        ShoptetConfigError: When a value is not one of :data:`KNOWN_FIELDS`.
    """
    spellings = {normalize.fold(label): str(label) for label in written}
    for label, field_name in folded.items():
        if field_name in KNOWN_FIELDS:
            continue
        valid = ", ".join(sorted(name for name in KNOWN_FIELDS if name))
        raise ShoptetConfigError(
            path,
            f"label_map[{spellings.get(label, label)!r}] = {field_name!r} is not a field; "
            f"valid fields are: {valid}",
        )


def build(config: dict[str, Any], path: Path) -> SiteAdapter:
    """Build one configured Shoptet shop.

    Args:
        config: The parsed TOML mapping.
        path: Where it came from, for error messages.

    Returns:
        The ready-to-use adapter.
    """
    return ShoptetSite(ShoptetConfig.from_mapping(config, path))


# --- microdata helpers -------------------------------------------------------


def _own(root: Tag, tag: Tag) -> bool:
    """Decide whether an element belongs to the page's own product.

    Args:
        root: The ``div.p-detail`` wrapper.
        tag: A candidate element found inside it.

    Returns:
        False when the element sits inside a related-product card.
    """
    for parent in tag.parents:
        if parent is root:
            return True
        if isinstance(parent, Tag) and parent.get("data-micro") == "product":
            return False
    return False


def _micro_all(root: Tag, prop: str) -> list[Tag]:
    """Return every microdata element of the page's own product.

    Args:
        root: The ``div.p-detail`` wrapper.
        prop: The ``itemprop`` name.

    Returns:
        The matching elements in document order.
    """
    return [tag for tag in root.select(f"[itemprop={prop}]") if _own(root, tag)]


def _micro(root: Tag, prop: str) -> str | None:
    """Return one microdata value of the page's own product.

    Args:
        root: The ``div.p-detail`` wrapper.
        prop: The ``itemprop`` name.

    Returns:
        The ``content`` attribute, the ``href``, or the visible text — whichever
        the element carries — or None.
    """
    tags = _micro_all(root, prop)
    return _value(tags[0]) if tags else None


def _value(tag: Tag) -> str | None:
    """Read a microdata element's value.

    Args:
        tag: The element.

    Returns:
        Its ``content``, ``href`` or text, or None when all three are empty.
    """
    return dom.attr(tag, "content") or dom.attr(tag, "href") or dom.text(tag)


# --- label collection --------------------------------------------------------


#: Where a Shoptet template states its parameters: the standard
#: ``.detail-parameters`` table, and the ``#product-detail``/``#product-detail-info``
#: pair the older "template-04" layout uses instead — which carries no class at
#: all, so it is matched on its id rather than on every ``<table>`` of the page.
_PARAMETER_TABLE_SELECTOR: Final = (
    "table.detail-parameters, .detail-parameters, dl.detail-parameters, table[id^='product-detail']"
)


def _parameter_rows(root: Tag) -> Iterator[tuple[str | None, str | None]]:
    """Yield every row of the shop's parameter tables and definition lists.

    Args:
        root: The ``div.p-detail`` wrapper.

    Yields:
        One ``(label, value)`` pair per row.
    """
    for table in root.select(_PARAMETER_TABLE_SELECTOR):
        if not _own(root, table):
            continue
        for row in table.select("tr"):
            cells = row.select("th, td")
            if len(cells) >= 2 and not _holds_variant_control(cells[1]):  # noqa: PLR2004
                # A cell that *is* the variant picker renders as its whole
                # widget ("Zvoľte variant Filter Espresso"); the placeholder-free
                # option list from :func:`_variant_axes` says the same thing.
                yield dom.text(cells[0]), dom.text(cells[1])
        terms = table.select("dt")
        definitions = table.select("dd")
        for term, definition in zip(terms, definitions, strict=False):
            yield dom.text(term), dom.text(definition)


#: Everything Shoptet renders a variant axis as: a ``<select>``, the newer
#: "advanced parameter" block of radio buttons with an image per value, or the
#: single combined select a shop with un-split variants shows
#: (``"Hmotnosť: 250g - Skladom (18 €)"`` in one option).
_PRICE_ID_SELECTOR: Final = "select[name='priceId']"
_VARIANT_CONTROL_SELECTOR: Final = (
    "select[data-parameter-id], select[name^='parameterValueId'], "
    f"{_PRICE_ID_SELECTOR}, div[data-parameter-id], div[class*='parameter-id-']"
)


def _holds_variant_control(cell: Tag) -> bool:
    """Say whether a parameter cell is really the variant picker.

    Args:
        cell: The value cell of a parameter row.

    Returns:
        True when the cell contains a select or a radio-button parameter block.
    """
    return cell.select_one(_VARIANT_CONTROL_SELECTOR) is not None


def _variant_controls(root: Tag) -> list[Tag]:
    """Return the variant pickers of the page's own product.

    Args:
        root: The ``div.p-detail`` wrapper.

    Returns:
        The controls, in page order, outermost first and never nested.
    """
    controls: list[Tag] = []
    for tag in root.select(_VARIANT_CONTROL_SELECTOR):
        if not _own(root, tag):
            continue
        if any(tag in already.descendants for already in controls):
            continue
        controls.append(tag)
    return controls


def _variant_axes(root: Tag) -> list[tuple[str | None, list[str]]]:
    """Return one ``(name, options)`` pair per variant axis, in page order.

    A shop may offer a weight axis and a roast axis at once; each one is read
    on its own so neither the placeholder ("Zvoľte variant") nor the other
    axis's values end up glued into a single string.

    Args:
        root: The ``div.p-detail`` wrapper.

    Returns:
        The axes that actually list options.
    """
    axes: list[tuple[str | None, list[str]]] = []
    for control in _variant_controls(root):
        options = _control_options(control)
        if options:
            axes.append((_control_label(control), options))
    return axes


def _control_label(control: Tag) -> str | None:
    """Return the human name of one variant axis.

    Args:
        control: The ``<select>`` or radio-button parameter block.

    Returns:
        Its ``data-parameter-name``, the surrounding row's label, or None.
    """
    name = dom.attr(control, "data-parameter-name")
    if name:
        return name
    wrapper = control.find_parent(class_="variant-list")
    if isinstance(wrapper, Tag):
        return dom.text(wrapper.select_one(".variant-label")) or dom.text(wrapper.select_one("th"))
    return None


#: What a shop writes into the option nobody may buy. A placeholder never
#: carries a price either, which is the second half of the test.
_PLACEHOLDER_PREFIXES: Final[tuple[str, ...]] = (
    "zvolte",
    "zvolte variant",
    "vyberte",
    "choose",
    "select",
)
#: The ``"(18 €)"`` a combined option ends in, and the ``"/250"`` a variant sku
#: ends in — the last resort for a weight the option text never states.
_OPTION_PRICE_RE: Final = re.compile(r"\(([^()]*\d[^()]*)\)\s*$")
#: Any run of whitespace, the non-breaking space Shoptet pads options with included.
_WHITESPACE_RE: Final = re.compile(r"\s+")
_SKU_WEIGHT_RE: Final = re.compile(r"[/\-](\d{2,5})\s*$")
#: The sizes coffee is actually bagged in. A sku suffix that is not one of them
#: is the shop numbering its packs: donfranko sells 250 g bags under the codes
#: 501, 510 and 520, and reading those as grams states a third of the real price
#: per kilogram. A weight the option text spells out in words is believed as is;
#: only a number guessed out of a sku has to look like a bag.
_PACK_SIZES_G: Final = frozenset(
    {
        100,
        125,
        150,
        200,
        220,
        227,
        250,
        300,
        330,
        340,
        350,
        400,
        450,
        500,
        600,
        700,
        750,
        800,
        900,
        1000,
        1200,
        1500,
        2000,
        2500,
        3000,
        5000,
    }
)
#: Availability words a combined option states next to the weight.
_IN_STOCK_WORDS: Final[tuple[str, ...]] = ("sklad", "in stock", "ihned", "dostupne", "dostupné")
_SOLD_OUT_WORDS: Final[tuple[str, ...]] = (
    "vypredane",
    "vyprodano",
    "vyprodane",
    "nedostupne",
    "sold out",
    "out of stock",
)


def _real_options(select: Tag) -> list[Tag]:
    """Return the options of a select that stand for a buyable variant.

    Args:
        select: The ``<select>`` element.

    Returns:
        The options, without the "choose a variant" placeholder.
    """
    return [option for option in select.select("option") if not _is_placeholder(option)]


def _is_placeholder(option: Tag) -> bool:
    """Say whether an option is the "choose a variant" prompt.

    Args:
        option: One ``<option>`` of a variant select.

    Returns:
        True when the option cannot be bought.
    """
    if not dom.attr(option, "value") or dom.attr(option, "data-choose"):
        return True
    label = dom.text(option) or ""
    if _OPTION_PRICE_RE.search(label) or dom.attr(option, "data-customerprice"):
        return False
    folded = normalize.fold(label)
    return any(folded.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES)


def _control_options(control: Tag) -> list[str]:
    """Return the real option labels of one variant axis.

    Args:
        control: The ``<select>`` or radio-button parameter block.

    Returns:
        The labels, without the "choose a variant" placeholder.
    """
    if control.name == "select":
        return dom.unique(dom.text(option) for option in _real_options(control))
    return dom.unique(
        dom.text(value) or dom.attr(value, "title")
        for value in control.select(".parameter-value, .advanced-parameter-inner")
    )


#: Every block a shop writes product prose into. ``.basic-description`` is the
#: Shoptet default, ``.p-short-description`` is the summary above it — which is
#: where a roastery just as often puts the whole coffee passport — and
#: ``.description-inner`` is what the "template-04" layout has instead of both.
_DESCRIPTION_SELECTOR: Final = ".basic-description, .p-short-description, .description-inner"


def _description_blocks(root: Tag) -> list[Tag]:
    """Return the long-description blocks of the page's own product.

    The blocks nest — ``.description-inner`` wraps ``.basic-description`` on the
    newer templates — so an inner block whose text an outer one already carries
    is dropped rather than read twice.

    Args:
        root: The ``div.p-detail`` wrapper.

    Returns:
        The blocks, in page order, outermost first and never nested.
    """
    blocks: list[Tag] = []
    for block in root.select(_DESCRIPTION_SELECTOR):
        if not _own(root, block):
            continue
        if any(block in kept.descendants for kept in blocks):
            continue
        blocks.append(block)
    return blocks


#: The two elements a bespoke fact row is built from: a label element followed
#: by a value element, with no colon anywhere.
_PAIR_LABEL_TAGS: Final[frozenset[str]] = frozenset({"span", "div", "dt", "th", "b", "strong"})
_PAIR_VALUE_TAGS: Final[frozenset[str]] = frozenset({"strong", "span", "div", "dd", "td"})
_PAIR_CELLS: Final = 2
#: The punctuation a fact label may end in without being a whole line.
_TRAILING_COLON: Final = " :"


def _pair_rows(root: Tag, label_map: dict[str, str]) -> Iterator[tuple[str | None, str | None]]:
    """Yield the label/value pairs of a hand-built spec block in the description.

    Roasteries routinely hand-write a "coffee passport" into the description
    instead of filling in the shop's parameter table, and reading the three
    shapes they reach for — classed label/value siblings, a two-column table,
    and a bespoke fact block — rescues a whole page of origin, process and
    altitude data on shops whose parameter table is empty.

    Args:
        root: The ``div.p-detail`` wrapper.
        label_map: Folded label -> field, used to keep the colon-less shape from
            claiming every two-element container on the page.

    Yields:
        One ``(label, value)`` pair per spec row.
    """
    for block in _description_blocks(root):
        yield from _classed_pairs(block)
        yield from _table_pairs(block)
        yield from _fact_pairs(block, label_map)


def _classed_pairs(block: Tag) -> Iterator[tuple[str | None, str | None]]:
    """Yield the pairs of sibling elements classed ``…label`` and ``…value``.

    Args:
        block: One description block.

    Yields:
        One ``(label, value)`` pair per spec row.
    """
    for label in block.select('[class*="label"]'):
        value = label.find_next_sibling()
        if not isinstance(value, Tag):
            continue
        if any("value" in token for token in dom.classes(value)):
            yield dom.text(label), dom.text(value)


def _table_pairs(block: Tag) -> Iterator[tuple[str | None, str | None]]:
    """Yield the rows of a two-column parameter table written into the prose.

    ``<tr><td>Farma</td><td>…</td></tr>`` states exactly what a parameter row
    states; only the shop's own table markup differs, and the line-by-line
    reader cannot see it because each cell lands on a line of its own.

    Args:
        block: One description block.

    Yields:
        One ``(label, value)`` pair per two-cell row.
    """
    for row in block.select("tr"):
        cells = row.select("th, td")
        if len(cells) == _PAIR_CELLS:
            yield dom.text(cells[0]), dom.text(cells[1])


def _fact_pairs(block: Tag, label_map: dict[str, str]) -> Iterator[tuple[str | None, str | None]]:
    """Yield the rows of a bespoke fact block, which states no colon at all.

    ``<div class="…__fact"><span>Země</span><strong>Peru</strong></div>`` is a
    row by layout rather than by punctuation, so the label map is what decides:
    a container is only read when its first child is short enough to be a
    parameter name *and* names one the shop's map knows.

    Args:
        block: One description block.
        label_map: Folded label -> field.

    Yields:
        One ``(label, value)`` pair per fact.
    """
    for container in block.find_all(name=True):
        children = [child for child in container.children if isinstance(child, Tag)]
        if len(children) != _PAIR_CELLS:
            continue
        label_tag, value_tag = children
        if label_tag.name not in _PAIR_LABEL_TAGS or value_tag.name not in _PAIR_VALUE_TAGS:
            continue
        label, value = dom.text(label_tag), dom.text(value_tag)
        if not label or not value or len(label.split()) > MAX_FUZZY_LABEL_WORDS:
            continue
        if ":" in label.rstrip(_TRAILING_COLON):
            # A colon *inside* the label means the element holds a whole
            # "LABEL: value" line, which the line reader splits correctly;
            # splitting it here would cut the value in two. A colon that merely
            # ends the label is punctuation, and dropping the row over it lost
            # every "<div>Pražení:</div><div>Světlé</div>" a shop writes.
            continue
        if map_label(normalize.fold(label), label_map):
            yield label, value


def _description_lines(root: Tag) -> list[str]:
    """Return the long description as the lines a reader sees.

    Args:
        root: The ``div.p-detail`` wrapper.

    Returns:
        One entry per non-empty line.
    """
    return [line for block in _description_blocks(root) for line in dom.lines(block)]


def _collect_labels(root: Tag, label_map: dict[str, str]) -> tuple[Labels, list[str]]:
    """Read every labelled value on the page, plus the prose left over.

    Args:
        root: The ``div.p-detail`` wrapper.
        label_map: Folded label -> field.

    Returns:
        The labels and the description lines that are not ``LABEL: value``.
    """
    labels = Labels()
    # Variant axes first: :class:`Labels` keeps the first value it is given, and
    # the axis list is the clean version of what the parameter table renders.
    for name, options in _variant_axes(root):
        labels.add(name, ", ".join(options), label_map)
    for label, value in _parameter_rows(root):
        labels.add(label, value, label_map)
    for label, value in _pair_rows(root, label_map):
        labels.add(label, value, label_map)
    pairs, prose = read_lines(_description_lines(root), label_map)
    for label, value in pairs:
        labels.add(label, value, label_map)
    return labels, prose


# --- field builders ----------------------------------------------------------


def _weight_options(root: Tag) -> list[str]:
    """Return the option labels of the select that chooses a package weight.

    Args:
        root: The ``div.p-detail`` wrapper.

    Returns:
        The labels, or an empty list when no select lists weights.
    """
    for _name, options in _variant_axes(root):
        if all(normalize.parse_weight_grams(option) for option in options):
            return options
    return []


def _offers(root: Tag) -> list[Tag]:
    """Return the ``schema.org/Offer`` blocks of the page's own product.

    Args:
        root: The ``div.p-detail`` wrapper.

    Returns:
        One element per offer, in page order.
    """
    return _micro_all(root, "offers")


def _offer_fields(offer: Tag) -> tuple[str | None, float | None, str | None, bool | None]:
    """Read one offer block.

    Args:
        offer: The ``[itemprop=offers]`` element.

    Returns:
        A ``(sku, price, currency, available)`` tuple.
    """
    sku = _first_value(offer, "sku")
    price = normalize.parse_amount(_first_value(offer, "price"))
    currency = _first_value(offer, "priceCurrency")
    availability = _first_value(offer, "availability")
    return sku, price, currency, schema_available(availability)


def _first_value(scope: Tag, prop: str) -> str | None:
    """Read the first microdata value inside an element.

    Args:
        scope: The element to search.
        prop: The ``itemprop`` name.

    Returns:
        The value, or None.
    """
    tag = scope.select_one(f"[itemprop={prop}]")
    return _value(tag) if tag is not None else None


def schema_available(availability: str | None) -> bool | None:
    """Turn a schema.org availability URL into a boolean.

    Args:
        availability: The URL or its last segment.

    Returns:
        True, False, or None when the page says nothing.
    """
    folded = normalize.fold(availability).replace(" ", "")
    if not folded:
        return None
    return "instock" in folded or "limitedavailability" in folded or "preorder" in folded


def _parse_variants(root: Tag, ref: ProductRef, currency: str | None) -> list[Variant]:
    """Build one variant per offer, naming it from the weight select.

    Shoptet renders one ``[itemprop=offers]`` block per purchasable variant in
    the same order as the weight select's options, so the two zip together when
    their lengths agree; when they do not, the offers still carry sku and price.

    Args:
        root: The ``div.p-detail`` wrapper.
        ref: The product reference being parsed.
        currency: The product's currency, used when an offer states none.

    Returns:
        The variants, or an empty list for a product without any.
    """
    combined = _combined_variants(root, ref, currency)
    if combined:
        return _drop_guessed_weights(combined)
    offers = _offers(root)
    options = _weight_options(root)
    if len(offers) <= 1 and not options:
        return []
    labels = _offer_labels(root, len(offers), options)
    variants = [
        _variant(offer, label, ref, currency) for offer, label in zip(offers, labels, strict=False)
    ]
    if not variants:
        variants = [
            Variant(url=ref.url, weight_g=normalize.parse_weight_grams(option), label=option)
            for option in options
        ]
    return _drop_guessed_weights(variants)


def _combined_variants(root: Tag, ref: ProductRef, currency: str | None) -> list[Variant]:
    """Build one variant per option of an un-split ``priceId`` select.

    A shop that never split its variants into parameters renders the whole
    catalogue of a product as one select whose options read
    ``"Hmotnosť: 250g - Skladom >5 ks (18 €)"``. Everything a variant needs is
    in that string, and reading it is the only way to get a weight: the offers
    beside it carry the sku alone, which is why these shops used to produce
    variants labelled ``BTE-250`` with no weight at all.

    Args:
        root: The ``div.p-detail`` wrapper.
        ref: The product reference being parsed.
        currency: The product's currency, used when an option states none.

    Returns:
        One variant per buyable option, or an empty list when the page has no
        such select.
    """
    selects = [select for select in root.select(_PRICE_ID_SELECTOR) if _own(root, select)]
    if not selects:
        return []
    options = _real_options(selects[0])
    offers = _offers(root)
    skus = [_first_value(offer, "sku") for offer in offers]
    if len(skus) != len(options):
        skus = [None] * len(options)
    return [
        _combined_variant(option, sku, ref, currency)
        for option, sku in zip(options, skus, strict=False)
    ]


def _combined_variant(
    option: Tag,
    sku: str | None,
    ref: ProductRef,
    currency: str | None,
) -> Variant:
    """Build one variant from one option of a combined select.

    Args:
        option: The ``<option>`` element.
        sku: The sku of the offer that stands for the same variant, when the
            two lists line up.
        ref: The product reference being parsed.
        currency: The product's currency, used when the option states none.

    Returns:
        The variant.
    """
    # Shoptet pads these options with non-breaking spaces; a label that reaches
    # the database should read the way the shop renders it.
    label = _WHITESPACE_RE.sub(" ", dom.text(option) or "").strip()
    price, detected = normalize.parse_price(_option_price(label))
    return Variant(
        external_id=sku,
        url=ref.url,
        weight_g=_option_weight(label, sku),
        price=price if price is not None else normalize.parse_amount(_option_amount(option)),
        currency=detected or currency,
        available=_option_available(label),
        label=label or sku,
    )


def _option_price(label: str) -> str | None:
    """Return the parenthesised amount a combined option ends in.

    Args:
        label: The option text.

    Returns:
        The amount with its currency, e.g. ``"18  €"``, or None.
    """
    match = _OPTION_PRICE_RE.search(label)
    return match.group(1) if match else None


def _option_amount(option: Tag) -> str | None:
    """Return the price Shoptet stamps on an option as an attribute.

    Args:
        option: The ``<option>`` element.

    Returns:
        The raw amount, or None.
    """
    return dom.attr(option, "data-customerprice")


def _option_weight(label: str, sku: str | None) -> int | None:
    """Read the package weight of one combined option.

    Args:
        label: The option text, which normally states the weight outright.
        sku: The matching sku, whose ``"706/250"`` suffix is the last resort.

    Returns:
        The weight in grams, or None.
    """
    grams = normalize.parse_weight_grams(label)
    if grams is None:
        match = _SKU_WEIGHT_RE.search(sku or "")
        grams = _sku_grams(int(match.group(1))) if match else None
    return grams if plausible_weight(grams) else None


def _sku_grams(number: int) -> int | None:
    """Believe a sku suffix as a weight only when it looks like a pack size.

    Coffee is sold in bags of known sizes. A suffix of 501, 510 or 520 is the
    shop numbering its packs, and reading it as grams puts a weight on a variant
    that no scale ever produced.

    Args:
        number: The number the suffix states.

    Returns:
        The weight in grams, or None when the number is not a pack size.
    """
    return number if number in _PACK_SIZES_G else None


def _drop_guessed_weights(variants: list[Variant]) -> list[Variant]:
    """Forget a variant weight that the sku suffix got wrong.

    The suffix of a sku like ``"01-01-03/100"`` is a pack code as often as it is
    a gram count, and reading the code as grams states a tenth of the real weight
    and ten times the real price per kilogram. One product's own prices settle
    it: a bigger bag never costs less than a smaller one, so a dearer variant
    that claims to hold less than a cheaper one is reading a code. Weights the
    option text states in words are left alone.

    Args:
        variants: The variants of one product, modified in place.

    Returns:
        The same list.
    """
    # Every comparison is made against the weights as the page stated them, and
    # only then are the losers cleared: deciding and clearing in one pass would
    # compare the next variant against a weight this loop had already removed.
    stated = [
        (variant, variant.price, variant.weight_g)
        for variant in variants
        if variant.price is not None and variant.weight_g is not None
    ]
    guessed = [
        variant
        for variant, price, weight in stated
        if normalize.parse_weight_grams(variant.label or "") is None
        and any(
            other_price < price and other_weight > weight
            for other, other_price, other_weight in stated
            if other is not variant
        )
    ]
    for variant in guessed:
        variant.weight_g = None
    return variants


def _option_available(label: str) -> bool | None:
    """Read the availability a combined option states in words.

    Args:
        label: The option text.

    Returns:
        True, False, or None when the option says nothing either way.
    """
    folded = normalize.fold(label)
    if any(word in folded for word in _SOLD_OUT_WORDS):
        return False
    return True if any(word in folded for word in _IN_STOCK_WORDS) else None


def _offer_labels(root: Tag, count: int, weight_options: list[str]) -> list[str | None]:
    """Name each offer after the variant combination it stands for.

    Shoptet renders one offer per purchasable combination, in the order the
    axes' options multiply out, so a shop with a weight axis *and* a roast axis
    produces four offers against two weight options — zipping the weights
    positionally would then leave every variant without a weight.

    Args:
        root: The ``div.p-detail`` wrapper.
        count: How many offers the page lists.
        weight_options: The options of the axis that lists weights, if any.

    Returns:
        One label per offer, ``None`` where nothing lines up.
    """
    axes = [options for _name, options in _variant_axes(root)]
    if axes:
        combinations = [" / ".join(parts) for parts in product(*axes)]
        if len(combinations) == count:
            return list(combinations)
    if len(weight_options) == count:
        return list(weight_options)
    return [None] * count


def _variant(offer: Tag, label: str | None, ref: ProductRef, currency: str | None) -> Variant:
    """Build one variant from an offer block.

    Args:
        offer: The ``[itemprop=offers]`` element.
        label: The matching weight option, when the two lists lined up.
        ref: The product reference being parsed.
        currency: The product's currency, used when the offer states none.

    Returns:
        The variant.
    """
    sku, price, offer_currency, available = _offer_fields(offer)
    weight = _option_weight(label or "", sku)
    return Variant(
        external_id=sku,
        url=ref.url,
        weight_g=weight,
        price=price,
        currency=offer_currency or currency,
        available=available,
        label=label or sku,
    )


def _parse_images(root: Tag, base_url: str) -> list[str]:
    """Collect the product photos.

    Args:
        root: The ``div.p-detail`` wrapper.
        base_url: The shop root, for relative hrefs.

    Returns:
        Absolute image URLs, de-duplicated, in page order.
    """
    candidates = [_micro(root, "image")]
    for selector in (".p-image a[href]", ".p-thumbnails a[href]"):
        candidates.extend(dom.attr(tag, "href") for tag in root.select(selector) if _own(root, tag))
    candidates.extend(
        dom.attr(image, "data-src") or dom.attr(image, "src")
        for image in root.select(".p-image img")
        if _own(root, image)
    )
    # A gallery's lightbox trigger is an anchor with "#" or nothing in its href,
    # which is not a photo; which of the two a shop writes even depends on the
    # HTML parser's version, so neither belongs in the record.
    return dom.unique(
        dom.absolute(base_url, url) for url in candidates if _is_image_href(url or "")
    )


def _is_image_href(href: str) -> bool:
    """Say whether an href points at a file rather than at the page itself.

    Args:
        href: The raw attribute value.

    Returns:
        True when it is worth resolving into an image URL.
    """
    cleaned = href.strip()
    if not cleaned or cleaned.startswith(("#", "javascript:", "data:")):
        return False
    return cleaned != "/"


def _parse_categories(soup: BeautifulSoup, root: Tag, name: str) -> list[str]:
    """Read the breadcrumb trail and the microdata category path.

    Args:
        soup: The whole page, because the breadcrumbs sit outside ``.p-detail``.
        root: The ``div.p-detail`` wrapper.
        name: The product name, dropped from the trail.

    Returns:
        The category names, without the home link and without the product.

    """
    crumbs = [
        dom.text(tag) or dom.attr(tag, "content")
        for tag in soup.select("[itemprop=itemListElement] [itemprop=name]")
    ]
    path = (_micro(root, "category") or "").split(">")
    folded_name = normalize.fold(name)
    combined = [*crumbs[1:], *path[1:]]
    return [
        value
        for value in dom.unique(combined)
        if normalize.fold(value) not in {folded_name, "domu", "domov", "home"}
    ]


def _parse_popularity(root: Tag) -> Popularity:
    """Read the aggregate rating Shoptet publishes as microdata.

    Args:
        root: The ``div.p-detail`` wrapper.

    Returns:
        The social-proof part of the model.
    """
    return Popularity(
        rating=normalize.parse_float(_micro(root, "ratingValue")),
        rating_max=normalize.parse_int(_micro(root, "bestRating")) or DEFAULT_RATING_MAX,
        review_count=normalize.parse_int(
            _micro(root, "reviewCount") or _micro(root, "ratingCount")
        ),
    )


def _short_description(root: Tag) -> str | None:
    """Return the shop's one-line product summary.

    Args:
        root: The ``div.p-detail`` wrapper.

    Returns:
        The text, or None.
    """
    block = root.select_one(".p-short-description")
    if block is not None and _own(root, block):
        return dom.text(block)
    return _micro(root, "description")


def _brand(labels: Labels, soup: BeautifulSoup) -> str | None:
    """Read who roasted the coffee, for a shop that sells more than one roaster.

    Args:
        labels: Every labelled value on the page.
        soup: The whole page, for the dataLayer and JSON-LD blocks a shop emits
            even when it renders no roaster row at all.

    Returns:
        The roaster's name, or None when the page names none.
    """
    for written, value in labels.raw.items():
        if normalize.fold(written) in _BRAND_LABELS:
            return value
    return _json_brand(soup)


def _json_brand(soup: BeautifulSoup) -> str | None:
    """Read the ``brand`` Shoptet writes into its dataLayer and JSON-LD blocks.

    Args:
        soup: The whole page.

    Returns:
        The brand name, or None when no script states one.
    """
    for script in soup.find_all("script"):
        match = _BRAND_JSON_RE.search(script.get_text())
        if match is None:
            continue
        quoted = match.group(1) or match.group(2)
        try:
            value = str(json.loads(quoted))
        except ValueError:  # a half-written script tag is not worth a crash
            continue
        if value.strip():
            return value.strip()
    return None


#: The product wrapper, whichever template the shop runs: ``.p-detail`` on every
#: current one, ``.p-detail-inner`` on the older "template-04" layout, which has
#: no ``.p-detail`` at all.
_ROOT_SELECTOR: Final = ".p-detail, .p-detail-inner"


class ShoptetSite(SiteAdapter):
    """One Shoptet shop, parameterised entirely by its :class:`ShoptetConfig`."""

    kind = PLATFORM

    def __init__(self, config: ShoptetConfig) -> None:
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

    def page_url(self, category_url: str, page: int) -> str:
        """Return the URL of one page of a category listing.

        Args:
            category_url: The category's first page.
            page: The 1-based page number.

        Returns:
            The absolute listing URL.
        """
        if page <= 1:
            return category_url
        if self.config.pagination == "query":
            separator = "&" if "?" in category_url else "?"
            return f"{category_url}{separator}page={page}"
        base = category_url if category_url.endswith("/") else f"{category_url}/"
        return f"{base}strana-{page}/"

    def parse_listing(self, html_text: str) -> list[ProductRef]:
        """Turn one listing page into product references.

        Only the real product grid is read: the "you might also like" carousel
        Shoptet renders above it repeats products from other pages.

        Args:
            html_text: The listing page source.

        Returns:
            One reference per product card, in page order.
        """
        soup = BeautifulSoup(html_text, "lxml")
        grid = soup.select_one("#products, .products-page")
        scope: Tag | BeautifulSoup = grid if grid is not None else soup
        cards = scope.select('[data-micro="product"]') or scope.select("div.product, div.p")
        refs = [self._listing_ref(card) for card in cards]
        return [ref for ref in refs if ref is not None]

    def _listing_ref(self, card: Tag) -> ProductRef | None:
        """Turn one product card into a reference.

        Args:
            card: The card element.

        Returns:
            The reference, or None when the card has no usable link.
        """
        link = card.select_one("a.name, a[data-micro=url], a.image, a[href]")
        url = dom.absolute(self.base_url, dom.attr(link, "href"))
        if url is None:
            logger.debug("skipping a %s product card without a link", self.site_id)
            return None
        external_id = (
            dom.attr(card, "data-micro-product-id")
            or dom.attr(card, "data-id")
            or url.rstrip("/").rsplit("/", 1)[-1]
        )
        offer = card.select_one('[data-micro="offer"]')
        price = normalize.parse_amount(dom.attr(offer, "data-micro-price"))
        currency = dom.attr(offer, "data-micro-price-currency") or self.config.currency
        if price is None:
            price, currency = self._listing_price(card, currency)
        image = card.select_one("[data-micro-image], img")
        return ProductRef(
            site_id=self.site_id,
            external_id=external_id,
            url=url,
            name=dom.text(card.select_one("[data-micro=name], a.name")),
            price=price,
            currency=currency if price is not None else None,
            image_url=dom.absolute(
                self.base_url,
                dom.attr(image, "data-micro-image") or dom.attr(image, "data-src"),
            ),
            extra=self._listing_extra(card),
        )

    def _listing_price(self, card: Tag, currency: str | None) -> tuple[float | None, str | None]:
        """Fall back to the rendered price when the card states no microdata.

        Args:
            card: The card element.
            currency: The currency found so far.

        Returns:
            A ``(price, currency)`` tuple.

        """
        text = dom.text(card.select_one(".price-final, .price"))
        price, detected = normalize.parse_price(text)
        return price, detected or currency

    def _listing_extra(self, card: Tag) -> dict[str, str]:
        """Collect the listing-only strings of one card.

        Args:
            card: The card element.

        Returns:
            Availability, flags and sku as plain strings.
        """
        extra: dict[str, str] = {}
        availability = dom.text(card.select_one(".availability"))
        if availability:
            extra["availability"] = availability
        flags = dom.unique(dom.text(flag) for flag in card.select(".flags-default .flag"))
        if flags:
            extra["flags"] = ", ".join(flags)
        sku = dom.text(card.select_one("[data-micro=sku]"))
        if sku:
            extra["sku"] = sku
        return extra

    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        """Walk every configured category and yield each product once.

        Args:
            fetcher: The shared polite fetcher.
            max_pages: Cap on listing pages per category, for this run only.

        Yields:
            One reference per product found.
        """
        cap = max_pages if max_pages is not None else self.max_pages
        seen: set[str] = set()
        for category_url in self.config.category_urls:
            yield from self._discover_category(fetcher, category_url, seen, cap)

    def _discover_category(
        self,
        fetcher: PoliteFetcher,
        category_url: str,
        seen: set[str],
        cap: int,
    ) -> Iterator[ProductRef]:
        """Walk the pagination of one category.

        Shoptet answers an out-of-range page with the category's own landing
        markup rather than a 404, so the walk stops on a page that lists nothing
        or that repeats the page before it. It must *not* stop merely because
        every product is already known: two categories overlap all the time, and
        a category whose first page is wholly contained in another one would
        otherwise never be paged past.

        Args:
            fetcher: The shared polite fetcher.
            category_url: The category's first page.
            seen: Ids already yielded, shared across categories.
            cap: How many pages of this category may be walked.

        Yields:
            One reference per product not yielded yet.
        """
        previous: set[str] | None = None
        for page in range(1, cap + 1):
            url = self.page_url(category_url, page)
            refs = self.parse_listing(fetcher.get(url).text)
            if not refs:
                logger.debug("%s: %s lists no products, stopping", self.site_id, url)
                return
            ids = {ref.external_id for ref in refs}
            if ids == previous:
                logger.debug("%s: %s repeats the previous page, stopping", self.site_id, url)
                return
            previous = ids
            fresh = [ref for ref in refs if ref.external_id not in seen]
            seen.update(ids)
            yield from fresh

    def parse_product(self, html: str, ref: ProductRef) -> Coffee | None:
        """Turn one detail page into a coffee.

        Args:
            html: The detail page source.
            ref: What the listing page already told us about this product.

        Returns:
            The parsed coffee, or None for cascara, merchandise and tasting packs.
        """
        soup = BeautifulSoup(html, "lxml")
        root = soup.select_one(_ROOT_SELECTOR) or soup.select_one('[itemtype$="/Product"]')
        if root is None:
            logger.warning("%s: no product block on %s", self.site_id, ref.url)
            return None
        name = self._name(soup, root, ref)
        if self.is_ignored(name):
            logger.debug("%s: %s is not coffee beans, skipping", self.site_id, name)
            return None
        return self._build(soup, root, ref, name)

    def _name(self, soup: BeautifulSoup, root: Tag, ref: ProductRef) -> str:
        """Read the product name.

        Args:
            soup: The whole page.
            root: The ``div.p-detail`` wrapper.
            ref: The product reference being parsed.

        Returns:
            The name, empty only when the page states none.
        """
        heading = root.select_one("h1") or soup.select_one("h1")
        return dom.text(heading) or ref.name or ""

    def _build(self, soup: BeautifulSoup, root: Tag, ref: ProductRef, name: str) -> Coffee:
        """Assemble the coffee once the page is known to be one.

        Args:
            soup: The whole page.
            root: The ``div.p-detail`` wrapper.
            ref: The product reference being parsed.
            name: The product name.

        Returns:
            The parsed coffee.
        """
        labels, prose = _collect_labels(root, self.label_map)
        # Open Graph and <meta> routinely name the origin or the cup notes the
        # visible markup omits; a real parameter row always wins.
        for key, value in dom.page_meta(soup).items():
            labels.raw.setdefault(key, value)
        brand = _brand(labels, soup)
        if brand is not None:
            labels.raw.setdefault(BRAND_KEY, brand)
        price, currency = self._price(root, ref)
        categories = _parse_categories(soup, root, name)
        species = parse_species(labels, name)
        variants = _parse_variants(root, ref, currency)
        summary = _short_description(root)
        return Coffee(
            site=self.site_id,
            external_id=_micro(root, "productID") or ref.external_id,
            url=ref.url,
            name=name,
            site_country=self.country,
            price=price,
            currency=currency,
            weight_g=headline_weight(
                labels,
                name,
                variants,
                price=price,
                # Shoptet's own "Hmotnost" is the parcel weight, packaging
                # included, so it is believed only when nothing else on the
                # page states a size.
                fallback=labels.get(F_SHIP_WEIGHT),
            ),
            available=schema_available(_micro(root, "availability")),
            decaf=is_decaf(labels, name, categories),
            origin=parse_origin(labels, name, blend=species.is_blend),
            processing=normalize.parse_processing(labels.get(F_PROCESS)),
            roast=parse_roast(labels, categories),
            species=species,
            taste=parse_taste(labels, summary),
            popularity=_parse_popularity(root),
            variants=variants,
            images=_parse_images(root, self.base_url),
            tags=dom.unique(
                dom.text(flag) for flag in root.select(".flags-default .flag") if _own(root, flag)
            ),
            categories=categories,
            certifications=option_list(labels.get(F_CERTIFICATIONS)),
            specialty_grade=specialty_grade(name, categories, score(labels)),
            original_price=self._original_price(root),
            description="\n".join(prose) or summary,
            origin_text=labels.get(F_COUNTRY),
            raw_attributes=labels.raw,
        )

    def _price(self, root: Tag, ref: ProductRef) -> tuple[float | None, str | None]:
        """Read the product's headline price.

        Args:
            root: The ``div.p-detail`` wrapper.
            ref: The product reference being parsed.

        Returns:
            A ``(price, currency)`` tuple; the currency is None without a price.
        """
        price = normalize.parse_amount(_micro(root, "price"))
        currency = _micro(root, "priceCurrency")
        if price is None:
            rendered = dom.text(root.select_one(".price-final"))
            price, detected = normalize.parse_price(rendered)
            currency = currency or detected
        price = price if price is not None else ref.price
        currency = currency or ref.currency or self.config.currency
        return price, currency if price is not None else None

    def _original_price(self, root: Tag) -> float | None:
        """Read the struck-through pre-discount price.

        Args:
            root: The ``div.p-detail`` wrapper.

        Returns:
            The price, or None when the product is not discounted.
        """
        for tag in root.select(".price-standard"):
            if _own(root, tag):
                return normalize.parse_amount(dom.text(tag))
        return None
