from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from coffee_aggregator import normalize
from coffee_aggregator.labels import (
    F_CERTIFICATIONS,
    F_COUNTRY,
    F_PROCESS,
    F_WEIGHT,
    KNOWN_FIELDS,
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
from coffee_aggregator.models import Coffee, Popularity, Variant
from coffee_aggregator.platforms.shoptet import BRAND_KEY
from coffee_aggregator.sites import html as dom
from coffee_aggregator.sites.base import DEFAULT_IGNORED, ProductRef, SiteAdapter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from coffee_aggregator.http import PoliteFetcher

logger = logging.getLogger(__name__)

PLATFORM: Final = "shopify"
DEFAULT_MAX_PAGES: Final = 20
#: Shopify's own ceiling for the JSON endpoints; a larger ``limit`` is clamped
#: by the platform anyway, so asking for more only makes the URL lie.
DEFAULT_LIMIT: Final = 250
MAX_LIMIT: Final = 250
PRODUCTS_PATH: Final = "products.json"
COLLECTIONS_PATH: Final = "collections.json"
PRODUCT_PATH: Final = "products/"

#: App-written tags (``__with:…`` bundles, ``__label:…`` badges) that describe
#: the storefront rather than the coffee.
_INTERNAL_TAG_PREFIX: Final = "__"
#: How many words a colon-less line may hold and still be read as a fact row.
_MAX_FACT_WORDS: Final = 8
#: How many leading words of a colon-less line are tried as its label.
_MAX_LABEL_WORDS: Final = 3
#: Zero-width characters a rich-text editor leaves inside a pasted value, and
#: the no-break space it pads one with. Neither is visible and both otherwise
#: end up in the database ("<zero-width><zero-width> Caturra").
_INVISIBLE: Final[dict[int, str]] = {
    0x200B: "",
    0x200C: "",
    0x200D: "",
    0xFEFF: "",
    0x00A0: " ",
}


def _clean(text: str | None) -> str:
    """Strip the invisible characters a rich-text editor leaves behind.

    Args:
        text: A label or a value as the shop wrote it.

    Returns:
        The same text without zero-width characters and with no-break spaces
        turned into ordinary ones.
    """
    return " ".join((text or "").translate(_INVISIBLE).split())


#: Shopify's own reading of two terms the shared vocabulary leaves out. A
#: ``Hmotnost:`` line inside ``body_html`` states what is in the bag; the parcel
#: weight is never published at all, so there is no Shoptet-style ambiguity.
PLATFORM_TERMS: Final[dict[str, str]] = {
    "hmotnost": F_WEIGHT,
    "weight": F_WEIGHT,
}

DEFAULT_LABEL_MAP: Final[dict[str, str]] = build_map(TERMS, extra=PLATFORM_TERMS)


class ShopifyConfigError(ValueError):
    """Raised when a shop TOML is missing a key ``ShopifySite`` cannot invent."""

    def __init__(self, path: Path, problem: str) -> None:
        """Build the error.

        Args:
            path: The configuration file that is wrong.
            problem: What is wrong with it.
        """
        super().__init__(f"{path}: {problem}")
        self.path = path


@dataclass(slots=True)
class ShopifyConfig:
    """Everything that differs between two Shopify shops.

    Attributes:
        site_id: Registry id, e.g. ``"coffia"``.
        name: Human-readable shop name.
        country: Which market the shop sells in.
        base_url: Shop root, used to build every JSON URL.
        currency: The currency the shop prices in. Required: ``products.json``
            quotes bare decimal strings and names no currency anywhere.
        platform: Always ``"shopify"``; kept so the mapping round-trips.
        collections: Collection handles to walk, most specific first. Empty
            means the whole catalogue.
        products_path: An explicit products JSON path that overrides both — for
            a shop whose coffee lives behind a hand-built path.
        label_map: Folded ``body_html`` label -> field, merged over
            :data:`DEFAULT_LABEL_MAP`.
        ignore: Extra folded markers for :meth:`ShopifySite.is_ignored`.
        max_pages: Hard cap on JSON pages per source.
        limit: Products per page, capped at Shopify's own 250.
    """

    site_id: str
    name: str
    country: Literal["CZ", "SK"]
    base_url: str
    currency: str
    platform: str = PLATFORM
    collections: list[str] = field(default_factory=list)
    products_path: str | None = None
    label_map: dict[str, str] = field(default_factory=dict)
    ignore: list[str] = field(default_factory=list)
    max_pages: int = DEFAULT_MAX_PAGES
    limit: int = DEFAULT_LIMIT

    @classmethod
    def from_mapping(cls, config: dict[str, Any], path: Path) -> ShopifyConfig:
        """Validate one parsed TOML mapping into a config.

        Args:
            config: The mapping ``tomllib`` produced.
            path: Where it came from, for error messages.

        Returns:
            The validated configuration.

        Raises:
            ShopifyConfigError: When a required key is missing or malformed.
        """
        required = ("site_id", "name", "country", "base_url", "currency")
        missing = [key for key in required if not config.get(key)]
        if missing:
            raise ShopifyConfigError(path, f"missing required key(s): {', '.join(missing)}")
        country = str(config["country"]).upper()
        if country not in {"CZ", "SK"}:
            raise ShopifyConfigError(path, f"country must be CZ or SK, not {country!r}")
        base_url = str(config["base_url"])
        label_map = {
            normalize.fold(key): str(value) for key, value in config.get("label_map", {}).items()
        }
        _check_label_map(label_map, config.get("label_map", {}), path)
        products_path = str(config["products_path"]) if config.get("products_path") else None
        return cls(
            site_id=str(config["site_id"]),
            name=str(config["name"]),
            country=cast("Literal['CZ', 'SK']", country),
            base_url=base_url if base_url.endswith("/") else f"{base_url}/",
            currency=str(config["currency"]),
            collections=[
                str(handle).strip("/ ")
                for handle in config.get("collections", [])
                if str(handle).strip("/ ")
            ],
            products_path=products_path,
            label_map=label_map,
            ignore=[normalize.fold(marker) for marker in config.get("ignore", [])],
            max_pages=int(config.get("max_pages", DEFAULT_MAX_PAGES)),
            limit=min(int(config.get("limit", DEFAULT_LIMIT)), MAX_LIMIT),
        )


def _check_label_map(folded: dict[str, str], written: dict[str, Any], path: Path) -> None:
    """Refuse a ``label_map`` that points a label at a field nobody reads.

    Args:
        folded: The folded label -> field mapping the TOML asked for.
        written: The same mapping with the labels as the file spells them.
        path: The configuration file, for the error message.

    Raises:
        ShopifyConfigError: When a value is not one of the shared
            ``KNOWN_FIELDS``, which every platform maps onto so one sink schema
            serves them all.
    """
    spellings = {normalize.fold(label): str(label) for label in written}
    for label, field_name in folded.items():
        if field_name in KNOWN_FIELDS:
            continue
        valid = ", ".join(sorted(name for name in KNOWN_FIELDS if name))
        raise ShopifyConfigError(
            path,
            f"label_map[{spellings.get(label, label)!r}] = {field_name!r} is not a field; "
            f"valid fields are: {valid}",
        )


def build(config: dict[str, Any], path: Path) -> SiteAdapter:
    """Build one configured Shopify shop.

    Args:
        config: The parsed TOML mapping.
        path: Where it came from, for error messages.

    Returns:
        The ready-to-use adapter.
    """
    return ShopifySite(ShopifyConfig.from_mapping(config, path))


# --- JSON helpers -------------------------------------------------------------


def _products(payload: str, url: str) -> list[dict[str, Any]]:
    """Read one ``products.json`` page.

    Args:
        payload: The response body.
        url: Where it came from, for the log line.

    Returns:
        The product objects the page lists, empty when the body is not the
        ``{"products": [...]}`` envelope Shopify publishes — which is what a
        password-protected storefront or an HTML 404 looks like from here.
    """
    try:
        parsed = json.loads(payload)
    except ValueError:
        logger.warning("%s: not JSON", url)
        return []
    if not isinstance(parsed, dict):
        return []
    entries = parsed.get("products")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _amount(raw: object) -> float | None:
    """Turn a Shopify price string into an amount of the shop's currency.

    Shopify quotes money as a plain decimal string in *major* units
    (``"389.00"``), never in minor units the way the WooCommerce Store API
    does — so there is no exponent to apply and no hundredfold trap.

    Args:
        raw: The ``price`` or ``compare_at_price`` value.

    Returns:
        The amount, or None when the field is empty or zero. ``"0.00"`` is
        Shopify for a gift card or a placeholder, never a free bag of coffee.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        value = normalize.parse_amount(text) or 0.0
    return value if value > 0 else None


def _variants(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the product's variant objects.

    Args:
        item: One ``products.json`` product object.

    Returns:
        The variants in Shopify's own order.
    """
    entries = item.get("variants")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _variant_weight(variant: dict[str, Any]) -> int | None:
    """Read one variant's pack weight.

    ``grams`` is the shipping weight the merchant typed into the variant, which
    for a bag of coffee *is* the pack weight and needs no parsing. It is left
    unset (``0``) often enough — coffia.sk never fills it — that the variant
    title stays the fallback.

    Args:
        variant: One entry of the product's ``variants`` array.

    Returns:
        The weight in grams, or None when neither source states one.
    """
    grams = normalize.parse_int(str(variant.get("grams") or ""))
    if plausible_weight(grams):
        return grams
    return normalize.parse_weight_grams(str(variant.get("title") or ""))


def _cheapest(variants: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the variant the headline price belongs to.

    Args:
        variants: The product's variant objects.

    Returns:
        The cheapest priced variant, or None when none states a price.
    """
    priced = [(amount, entry) for entry in variants if (amount := _amount(entry.get("price")))]
    if not priced:
        return None
    return min(priced, key=lambda pair: pair[0])[1]


def _images(item: dict[str, Any]) -> list[str]:
    """Collect the product photos.

    Args:
        item: One ``products.json`` product object.

    Returns:
        Full-size image URLs, de-duplicated, in Shopify's own order.
    """
    entries = item.get("images")
    if not isinstance(entries, list):
        return []
    return dom.unique(
        str(entry.get("src")) for entry in entries if isinstance(entry, dict) and entry.get("src")
    )


def _tags(item: dict[str, Any]) -> list[str]:
    """Return the product's tags, without the ones an app wrote.

    Args:
        item: One ``products.json`` product object.

    Returns:
        The tag names, de-duplicated, in Shopify's own order.
    """
    raw = item.get("tags")
    entries = raw if isinstance(raw, list) else normalize.split_list(str(raw or ""))
    return dom.unique(str(tag) for tag in entries if not str(tag).startswith(_INTERNAL_TAG_PREFIX))


# --- body_html label reading --------------------------------------------------


def _pairs_of(line: str, label_map: dict[str, str]) -> list[tuple[str, str]]:
    """Read every ``Label: value`` a single description line states.

    Three shapes, all of them common: the ordinary one line per fact, the
    pipe-joined run ``"Region: … | Odrůda: … | Zpracování: …"`` that
    penguincoffee.cz writes, and the colon-less ``"Odroda Villalobos"`` that
    coffia.sk writes. Only the first needs no guard; the other two are gated so
    ordinary prose cannot pose as a parameter row.

    Args:
        line: One line of the rendered description.
        label_map: Folded label -> field, already merged with the defaults.

    Returns:
        The pairs the line states, empty when it is prose.
    """
    segments = [segment.strip() for segment in line.split("|")]
    if len(segments) > 1:
        matched = [dom.LABEL_RE.match(segment) for segment in segments]
        if all(match is not None for match in matched):
            return [
                (match.group("label").strip(), match.group("value").strip())
                for match in matched
                if match is not None
            ]
    single = dom.LABEL_RE.match(line)
    if single is not None:
        return [(single.group("label").strip(), single.group("value").strip())]
    return _colonless_pair(line, label_map)


def _colonless_pair(line: str, label_map: dict[str, str]) -> list[tuple[str, str]]:
    """Read a fact row that states its label without a colon.

    ``"Výška 1700-1800"`` is a parameter row by layout alone, so the label map
    is what decides: only a short line whose first words name a field the shop
    knows is read this way, and everything else stays prose.

    Args:
        line: One line of the rendered description.
        label_map: Folded label -> field.

    Returns:
        A one-element list, or an empty one when the line is prose.
    """
    words = line.split()
    if not words or len(words) > _MAX_FACT_WORDS:
        return []
    fuzzy: int | None = None
    exact: int | None = None
    for count in range(1, min(_MAX_LABEL_WORDS, len(words) - 1) + 1):
        folded = normalize.fold(" ".join(words[:count]))
        if folded in label_map:
            # An exact vocabulary entry wins however long it is: "Cupping score"
            # names the score, where the one-word "Cupping" leaves "score" in
            # the value.
            exact = count
        elif fuzzy is None and map_label(folded, label_map):
            # A fuzzy hit takes the *fewest* words it can: "Finca La Isabel,
            # West Valley" names the farm "La Isabel, West Valley", and a
            # greedy label would eat the farm's own name.
            fuzzy = count
    chosen = exact if exact is not None else fuzzy
    if chosen is None:
        return []
    return [(" ".join(words[:chosen]), " ".join(words[chosen:]))]


def _read_description(
    markup: str | None,
    label_map: dict[str, str],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Split one ``body_html`` into labelled values and marketing prose.

    Args:
        markup: The product's ``body_html``.
        label_map: Folded label -> field.

    Returns:
        Every pair the description states, and every line that is not one.
    """
    if not markup:
        return ([], [])
    soup = BeautifulSoup(markup, "lxml")
    pairs: list[tuple[str, str]] = []
    for row in soup.select("tr"):
        cells = row.select("th, td")
        if len(cells) >= 2:  # noqa: PLR2004  (a label and its value)
            pairs.append((_clean(dom.text(cells[0])), _clean(dom.text(cells[1]))))
    lines = [_clean(line) for line in dom.lines(soup)]
    found, prose = read_lines(lines, label_map, _pairs_of)
    pairs.extend(found)
    return (pairs, prose)


class ShopifySite(SiteAdapter):
    """One Shopify shop, parameterised entirely by its :class:`ShopifyConfig`.

    Every Shopify storefront publishes its whole catalogue as JSON with no
    authentication and no key, so a crawl costs one request per 250 products
    and never fetches a detail page: discovery hands the product's own JSON to
    the pipeline in :attr:`ProductRef.payload`.
    """

    kind = PLATFORM

    def __init__(self, config: ShopifyConfig) -> None:
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

    def sources(self) -> list[str]:
        """Return the products JSON paths one crawl walks.

        Returns:
            The explicit path when the TOML pins one, otherwise one path per
            configured collection, otherwise the whole catalogue.
        """
        if self.config.products_path:
            return [self.config.products_path.lstrip("/")]
        if self.config.collections:
            return [f"collections/{handle}/{PRODUCTS_PATH}" for handle in self.config.collections]
        return [PRODUCTS_PATH]

    def page_url(self, source: str, page: int) -> str:
        """Return the URL of one page of one products JSON source.

        Args:
            source: A shop-relative products JSON path.
            page: The 1-based page number.

        Returns:
            The absolute URL.
        """
        query = urlencode({"limit": self.config.limit, "page": page})
        return f"{urljoin(self.base_url, source)}?{query}"

    def collections_url(self) -> str:
        """Return the URL that lists the shop's collections.

        Returns:
            The absolute URL; useful when writing a shop's TOML, not during a
            crawl.
        """
        return urljoin(self.base_url, COLLECTIONS_PATH)

    def product_url(self, item: dict[str, Any]) -> str:
        """Return the storefront URL of one product.

        Args:
            item: One ``products.json`` product object.

        Returns:
            The absolute URL, built from the handle — the JSON states no
            permalink of its own.
        """
        return urljoin(self.base_url, f"{PRODUCT_PATH}{item.get('handle') or ''}")

    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        """Walk every configured source and yield each product once.

        Args:
            fetcher: The shared polite fetcher.
            max_pages: Cap on JSON pages per source, for this run only.

        Yields:
            One reference per product found; each carries the product's own
            JSON in ``payload``, so the pipeline never fetches its URL.
        """
        cap = max_pages if max_pages is not None else self.max_pages
        seen: set[str] = set()
        for source in self.sources():
            yield from self._discover_source(fetcher, source, seen, cap)

    def _discover_source(
        self,
        fetcher: PoliteFetcher,
        source: str,
        seen: set[str],
        cap: int,
    ) -> Iterator[ProductRef]:
        """Page through one products JSON source.

        Shopify answers a page past the end with ``{"products": []}`` rather
        than a 404, and publishes no count anywhere in the body — the total
        only ever appears as a header, which :class:`~coffee_aggregator.http.
        FetchResult` does not expose and which a collection's cached
        ``products_count`` contradicts anyway. The empty page is therefore the
        only stop condition.

        Args:
            fetcher: The shared polite fetcher.
            source: A shop-relative products JSON path.
            seen: Ids already yielded, shared across sources.
            cap: How many pages of this source may be walked.

        Yields:
            One reference per product not yielded yet.
        """
        for page in range(1, cap + 1):
            url = self.page_url(source, page)
            items = _products(fetcher.get(url).text, url)
            if not items:
                logger.debug("%s: %s lists no products, stopping", self.site_id, url)
                return
            for item in items:
                ref = self._ref(item)
                if ref is None or ref.external_id in seen:
                    continue
                seen.add(ref.external_id)
                yield ref

    def _ref(self, item: dict[str, Any]) -> ProductRef | None:
        """Turn one product object into a reference carrying its own JSON.

        Args:
            item: One ``products.json`` product object.

        Returns:
            The reference, or None when the product is not coffee beans or
            states neither an id nor a handle.
        """
        handle = str(item.get("handle") or "")
        external_id = str(item.get("id") or handle)
        if not external_id:
            logger.debug("%s: a product states neither id nor handle", self.site_id)
            return None
        name = str(item.get("title") or "")
        if self.is_ignored(name):
            logger.debug("%s: %s is not coffee beans, skipping", self.site_id, name)
            return None
        cheapest = _cheapest(_variants(item))
        price = _amount(cheapest.get("price")) if cheapest else None
        images = _images(item)
        return ProductRef(
            site_id=self.site_id,
            external_id=external_id,
            url=self.product_url(item),
            name=name or None,
            price=price,
            currency=self.config.currency if price is not None else None,
            image_url=images[0] if images else None,
            payload=json.dumps(item, ensure_ascii=False),
        )

    # --- parsing -------------------------------------------------------------

    def parse_product(self, html: str, ref: ProductRef) -> Coffee | None:
        """Turn one product's JSON into a coffee.

        Args:
            html: The payload discovery captured; the pipeline hands back
                exactly what ``ref.payload`` held.
            ref: What discovery already told us about this product.

        Returns:
            The parsed coffee, or None for cascara, merchandise and tasting
            packs — and for a payload that turns out to be unreadable.
        """
        try:
            item = json.loads(ref.payload if ref.payload is not None else html)
        except ValueError:
            logger.warning("%s: unreadable payload for %s", self.site_id, ref.url)
            return None
        if not isinstance(item, dict):
            return None
        name = str(item.get("title") or ref.name or "")
        if self.is_ignored(name):
            logger.debug("%s: %s is not coffee beans, skipping", self.site_id, name)
            return None
        return self._build(item, ref, name)

    def _labels(self, item: dict[str, Any]) -> tuple[Labels, list[str]]:
        """Read every labelled value the product's description states.

        Args:
            item: One ``products.json`` product object.

        Returns:
            The labels and the description prose.
        """
        labels = Labels()
        pairs, prose = _read_description(str(item.get("body_html") or ""), self.label_map)
        for label, value in pairs:
            labels.add(label, value, self.label_map)
        return labels, prose

    def _build(self, item: dict[str, Any], ref: ProductRef, name: str) -> Coffee:
        """Assemble the coffee once the payload is known to be one.

        Args:
            item: One ``products.json`` product object.
            ref: The product reference being parsed.
            name: The product name.

        Returns:
            The parsed coffee.
        """
        labels, prose = self._labels(item)
        entries = _variants(item)
        variants = self._variants(item, entries)
        cheapest = _cheapest(entries)
        price = _amount(cheapest.get("price")) if cheapest else None
        vendor = str(item.get("vendor") or "").strip()
        if vendor:
            # The model has no brand column yet, and a Shopify catalogue is very
            # often a multi-roaster one, so the vendor is kept where a typed
            # column can read it later.
            labels.raw.setdefault(BRAND_KEY, vendor)
        tags = _tags(item)
        # Shopify's only taxonomy is the product type plus the merchant's own
        # tags; both name the brewing style and the decaf flag often enough that
        # the roast and decaf readers need them together.
        categories = dom.unique([str(item.get("product_type") or ""), *tags])
        species = parse_species(labels, name)
        return Coffee(
            site=self.site_id,
            external_id=str(item.get("id") or ref.external_id),
            url=self.product_url(item) if item.get("handle") else ref.url,
            name=name,
            site_country=self.country,
            price=price,
            currency=self.config.currency if price is not None else None,
            weight_g=headline_weight(labels, name, variants, price=price),
            available=any(entry.get("available") for entry in entries) if entries else None,
            decaf=is_decaf(labels, name, categories),
            origin=parse_origin(labels, name, blend=species.is_blend),
            processing=normalize.parse_processing(labels.get(F_PROCESS)),
            # A Shopify variant axis is very often the brewing style itself
            # ("Filter 250g" / "Old School Espresso 1kg"), which on these shops
            # is the only place the roast profile is stated at all. The variant
            # labels feed the profile reader only; they are not categories.
            roast=parse_roast(
                labels,
                [*categories, *(variant.label or "" for variant in variants)],
            ),
            species=species,
            taste=parse_taste(labels, None),
            popularity=Popularity(),
            variants=variants,
            images=_images(item),
            tags=tags,
            categories=categories,
            certifications=option_list(labels.get(F_CERTIFICATIONS)),
            specialty_grade=specialty_grade(name, categories, score(labels)),
            original_price=_original_price(cheapest, price),
            description="\n".join(prose) or None,
            origin_text=labels.get(F_COUNTRY),
            raw_attributes=labels.raw,
        )

    def _variants(self, item: dict[str, Any], entries: list[dict[str, Any]]) -> list[Variant]:
        """Build one variant per Shopify variant.

        Unlike the WooCommerce Store API, Shopify states a price, a weight and
        a stock flag for every single variant, so a shop's whole price ladder
        arrives in the listing response.

        Args:
            item: One ``products.json`` product object.
            entries: The product's variant objects.

        Returns:
            The variants, in Shopify's own order.
        """
        url = self.product_url(item)
        return [
            Variant(
                external_id=str(entry.get("id")) if entry.get("id") else None,
                url=url,
                weight_g=_variant_weight(entry),
                price=_amount(entry.get("price")),
                currency=self.config.currency,
                available=bool(entry["available"]) if "available" in entry else None,
                label=str(entry.get("title") or "") or None,
            )
            for entry in entries
        ]


def _original_price(cheapest: dict[str, Any] | None, price: float | None) -> float | None:
    """Return the pre-discount price, when the product is actually discounted.

    Args:
        cheapest: The variant the headline price belongs to.
        price: The price that variant sells for now.

    Returns:
        The ``compare_at_price``, or None when it is not higher than the
        current one.
    """
    if cheapest is None or price is None:
        return None
    regular = _amount(cheapest.get("compare_at_price"))
    if regular is None or regular <= price:
        return None
    return regular
