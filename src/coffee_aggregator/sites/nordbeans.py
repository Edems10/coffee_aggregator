from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from coffee_aggregator import normalize
from coffee_aggregator.models import (
    Coffee,
    Origin,
    Roast,
    Species,
    Taste,
    Variant,
)
from coffee_aggregator.sites import html as dom
from coffee_aggregator.sites.base import DEFAULT_IGNORED, ProductRef, SiteAdapter
from coffee_aggregator.sites.registry import register

if TYPE_CHECKING:
    from collections.abc import Iterator

    from coffee_aggregator.http import PoliteFetcher

logger = logging.getLogger(__name__)

BASE_URL: Final = "https://www.nordbeans.cz/"
#: The shop has no paginated bean category; it has three bean categories, and
#: ``max_pages`` caps how many of them one run walks.
CATEGORY_PATHS: Final = ("/espresso/", "/filtrovana-kava/", "/decaf/")
SITEMAP_PATH: Final = "/1/sitemap_products.xml"
DEFAULT_CURRENCY: Final = "CZK"
#: Folded markers of products the bean categories list that are not beans.
IGNORED_MARKERS: Final = (
    *DEFAULT_IGNORED,
    "kapsle",
    "four pack",
    "darkovy",
    "poukaz",
    "degustacni",
)

_PRODUCT_ID_RE: Final = re.compile(r"_z(?P<id>\d+)/?(?:[?#]|$)")
#: ``page_data = {...};page_data[`` — the detail page's own GA4 product record.
_PAGE_DATA_RE: Final = re.compile(r"page_data\s*=\s*(?P<json>\{.*?\})\s*;\s*page_data\[", re.DOTALL)
#: ``gtm_prva[1417] = {'id': ..., 'price': ...};`` — one entry per package size.
_VARIATION_RE: Final = re.compile(r"gtm_prva\[(?P<id>\d+)\]\s*=\s*\{(?P<fields>[^}]*)\}")
_VARIATION_FIELD_RE: Final = re.compile(r"'(?P<key>\w+)'\s*:\s*'?(?P<value>[^',}]*)'?")
#: The shop writes its cup notes as ``a - b - c``, which the shared splitter
#: leaves whole because a bare dash also joins words. Candidate for sites/html.py.
_NOTE_SPLIT_RE: Final = re.compile(r"\s+[\u2010-\u2015\u2212-]\s+")

_LABEL_SERIES: Final = "rada"
_LABEL_BREWING: Final = "priprava"
_LABEL_TASTE: Final = "chut"
_LABEL_NOTES: Final = "charakteristika"
_LABEL_BODY: Final = "intenzita tela"
_LABEL_ACIDITY: Final = "intenzita kyselosti"
_LABEL_BITTERNESS: Final = "intenzita horkosti"
_LABEL_SWEETNESS: Final = "intenzita sladkosti"
_LABEL_PROCESS: Final = "zpracovani"
_LABEL_VARIETY: Final = "odruda"
_LABEL_ORIGIN: Final = "puvod"
_LABEL_REGION: Final = "region"
_LABEL_LOCALITY: Final = "lokalita"
_LABEL_STATION: Final = "zpracovatelsky zavod"
_LABEL_ALTITUDE: Final = "nadm. vyska"
_LABEL_HARVEST: Final = "sklizen"
_LABEL_ROAST: Final = "prazeni"
_LABEL_SCA: Final = "sca skore"
_LABEL_PRODUCER: Final = "farmar"

#: Folded parameter label -> the :class:`~coffee_aggregator.models.Taste` field
#: its 1-5 bean bar fills in.
_INTENSITY_LABELS: Final = {
    _LABEL_BODY: "body",
    _LABEL_ACIDITY: "acidity",
    _LABEL_BITTERNESS: "bitterness",
    _LABEL_SWEETNESS: "sweetness",
}


def external_id_of(url: str | None) -> str | None:
    """Read the numeric product id out of a ``..._z<id>/`` URL.

    Args:
        url: A product URL or href.

    Returns:
        The id as a string, or None when the URL is not a product URL.
    """
    if not url:
        return None
    match = _PRODUCT_ID_RE.search(url)
    return match.group("id") if match else None


def split_notes(text: str | None) -> list[str]:
    """Split a dash-separated or comma-separated enumeration of cup notes.

    Args:
        text: Text such as ``"visne - med - cerny rybiz"``.

    Returns:
        The individual notes, de-duplicated, in their original order.
    """
    if not text:
        return []
    items = [item for chunk in _NOTE_SPLIT_RE.split(text) for item in normalize.split_list(chunk)]
    return dom.unique(items)


def _tracking(tag: Tag | None, attribute: str = "data-tracking-click") -> dict[str, object]:
    """Decode the GA4 record the shop hangs off every product link.

    Args:
        tag: The ``a.product-link`` element.
        attribute: Which tracking attribute to read.

    Returns:
        The first product record, or an empty mapping.
    """
    raw = dom.attr(tag, attribute)
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("a product card carried unreadable tracking data")
        return {}
    if not isinstance(decoded, dict):
        return {}
    click = decoded.get("click") if isinstance(decoded.get("click"), dict) else decoded
    products = click.get("products") if isinstance(click, dict) else None
    if isinstance(products, list) and products and isinstance(products[0], dict):
        return products[0]
    return {}


def _page_data(soup: BeautifulSoup) -> dict[str, object]:
    """Read the ``page_data`` record a detail page pushes into the data layer.

    Args:
        soup: The parsed detail page.

    Returns:
        The first product record, or an empty mapping.
    """
    for script in soup.find_all("script"):
        match = _PAGE_DATA_RE.search(script.get_text())
        if match is None:
            continue
        try:
            decoded = json.loads(match.group("json"))
        except json.JSONDecodeError:
            logger.debug("the page_data script was not valid JSON")
            continue
        products = decoded.get("products") if isinstance(decoded, dict) else None
        if isinstance(products, list) and products and isinstance(products[0], dict):
            return products[0]
    return {}


def _number(value: object) -> float | None:
    """Narrow a decoded JSON value to a number.

    Args:
        value: Anything ``json.loads`` produced.

    Returns:
        The number, or None when the value is not numeric.
    """
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _text_of(value: object) -> str | None:
    """Narrow a decoded JSON value to a non-empty string.

    Args:
        value: Anything ``json.loads`` produced.

    Returns:
        The trimmed string, or None.
    """
    return value.strip() or None if isinstance(value, str) else None


def _parse_params(soup: BeautifulSoup) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    """Read the ``Detaily`` parameter list, bean bars included.

    Each row is ``<li><strong>LABEL</strong><span>VALUE</span></li>``; an
    intensity row instead draws five bean icons, of which the active ones are
    the value on the shop's own 1-5 scale.

    Args:
        soup: The parsed detail page.

    Returns:
        A ``(raw_attributes, folded_lookup, intensities)`` tuple.
    """
    raw: dict[str, str] = {}
    folded: dict[str, str] = {}
    intensities: dict[str, int] = {}
    for row in soup.select("div.product-params li"):
        label = dom.text(row.select_one("strong"))
        if not label:
            continue
        key = normalize.fold(label)
        bar = row.select_one("p.intensity")
        # The shop glues its units on with a no-break space; dash_fold_keep turns
        # that into a plain one without touching case or diacritics.
        value = (
            normalize.dash_fold_keep(
                dom.text(bar.select_one("span")) if bar else dom.text(row.select_one("span"))
            )
            or None
        )
        if bar is not None:
            active = bar.select("span.icons_bean:not(.inactive)")
            if active:
                intensities[key] = len(active)
        if value:
            raw.setdefault(label.strip().upper(), value)
            folded.setdefault(key, value)
    return raw, folded, intensities


def _parse_variants(soup: BeautifulSoup, ref: ProductRef) -> list[Variant]:
    """Read the package-size switcher out of the inline GA4 variation map.

    Args:
        soup: The parsed detail page.
        ref: The reference being parsed.

    Returns:
        One variant per package size, in page order.
    """
    variants: list[Variant] = []
    seen: set[str] = set()
    for script in soup.find_all("script"):
        for match in _VARIATION_RE.finditer(script.get_text()):
            identifier = match.group("id")
            if identifier in seen:
                continue
            seen.add(identifier)
            fields = {
                field.group("key"): field.group("value").strip()
                for field in _VARIATION_FIELD_RE.finditer(match.group("fields"))
            }
            label = fields.get("variationName")
            price = normalize.parse_amount(fields.get("price"))
            variants.append(
                Variant(
                    external_id=fields.get("id") or identifier,
                    url=f"{ref.url}#{identifier}",
                    weight_g=normalize.parse_weight_grams(label),
                    price=price,
                    currency=DEFAULT_CURRENCY if price is not None else None,
                    label=label,
                )
            )
    return variants


def _parse_availability(soup: BeautifulSoup, record: dict[str, object]) -> bool | None:
    """Decide whether the product is in stock.

    Args:
        soup: The parsed detail page.
        record: The ``page_data`` product record.

    Returns:
        True, False, or None when neither source says.
    """
    sold_out = _number(record.get("soldOut"))
    stated = normalize.fold(_text_of(record.get("availability")))
    delivery = normalize.fold(dom.text(soup.select_one("[data-deliverytime]")))
    blob = f"{stated} {delivery}"
    if "skladem" in blob and "neni skladem" not in blob:
        return True
    if "vyprodano" in blob or "nedostupne" in blob or "neni skladem" in blob:
        return False
    if sold_out is not None and sold_out > 0:
        return False
    return None


def _parse_images(soup: BeautifulSoup) -> list[str]:
    """Collect the product photos from the gallery and the Open Graph tag.

    Args:
        soup: The parsed detail page.

    Returns:
        Absolute image URLs, in page order.
    """
    candidates = [
        *(dom.attr(link, "href") for link in soup.select("div.product-gallery a[href]")),
        *(dom.attr(image, "src") for image in soup.select("div.product-gallery img[src]")),
        dom.attr(soup.select_one('meta[property="og:image"]'), "content"),
    ]
    return dom.unique(
        dom.absolute(BASE_URL, url) for url in candidates if url and "/data/tmp/" in url
    )


def _card_extra(card: Tag, record: dict[str, object]) -> dict[str, str]:
    """Collect the listing-only strings of one product card.

    Args:
        card: The ``div.catalog`` element wrapping the link.
        record: The card's GA4 record.

    Returns:
        Origin, cup notes, roast profile, flags and stock as plain strings.
    """
    extra: dict[str, str] = {}
    columns = [
        [text for text in (dom.text(item) for item in column.select("p")) if text]
        for column in card.select("div.catalog-data > div")
    ]
    facts = columns[0] if columns else []
    if facts:
        extra["origin"] = facts[0]
    if len(facts) > 1:
        extra["flavor_notes"] = facts[1]
    if len(columns) > 1 and columns[1]:
        extra["profile"] = columns[1][0]
    flags = dom.unique(dom.text(flag) for flag in card.select("div.catalog-flags span.flag"))
    if flags:
        extra["flags"] = ", ".join(flags)
    availability = _text_of(record.get("availability"))
    if availability:
        extra["stock"] = availability
    producer = _text_of(record.get("producer"))
    if producer:
        extra["producer"] = producer
    return extra


@register
class NordbeansSite(SiteAdapter):
    """nordbeans.cz — a Czech roastery on a bespoke, server-rendered platform."""

    site_id = "nordbeans"
    name = "Nordbeans"
    country = "CZ"
    base_url = BASE_URL
    kind = "roaster"
    #: One per bean category; the shop paginates none of them.
    max_pages = len(CATEGORY_PATHS)

    def ignored_names(self) -> tuple[str, ...]:
        """Return the folded markers of products that are not whole beans.

        Returns:
            The default markers plus the shop's capsules, packs and vouchers.
        """
        return IGNORED_MARKERS

    def category_url(self, index: int) -> str:
        """Return the URL of one bean category.

        Args:
            index: The 0-based position in :data:`CATEGORY_PATHS`.

        Returns:
            The absolute listing URL.
        """
        return urljoin(BASE_URL, CATEGORY_PATHS[index])

    def parse_listing(self, html: str) -> list[ProductRef]:
        """Turn one category page into product references.

        Every reference carries the real ``a.product-link`` href, so no slug is
        ever rebuilt from a name.

        Args:
            html: The listing page source.

        Returns:
            One reference per product card, in page order.
        """
        soup = BeautifulSoup(html, "lxml")
        refs: list[ProductRef] = []
        for link in soup.select("a.product-link[href]"):
            url = dom.absolute(BASE_URL, dom.attr(link, "href"))
            external_id = external_id_of(url)
            if url is None or external_id is None:
                logger.debug("skipping a product card without a product link")
                continue
            record = _tracking(link)
            price = _number(record.get("priceWithVat"))
            card = link.find_parent("div", class_="catalog")
            refs.append(
                ProductRef(
                    site_id=self.site_id,
                    external_id=external_id,
                    url=url,
                    name=dom.text(link.select_one("h3.title")) or _text_of(record.get("name")),
                    price=price,
                    currency=DEFAULT_CURRENCY if price is not None else None,
                    image_url=dom.absolute(BASE_URL, dom.attr(link.select_one("img"), "src")),
                    extra=_card_extra(card, record) if isinstance(card, Tag) else {},
                )
            )
        return refs

    def parse_sitemap(self, xml: str) -> list[ProductRef]:
        """Turn the product sitemap into references.

        Args:
            xml: The ``sitemap_products.xml`` body.

        Returns:
            One reference per ``_z<id>/`` location. The sitemap covers the whole
            catalogue, mugs and grinders included, so the category walk stays
            the default.
        """
        soup = BeautifulSoup(xml, "xml")
        refs: list[ProductRef] = []
        seen: set[str] = set()
        for location in soup.find_all("loc"):
            url = location.get_text(strip=True)
            external_id = external_id_of(url)
            if external_id is None or external_id in seen:
                continue
            seen.add(external_id)
            refs.append(ProductRef(site_id=self.site_id, external_id=external_id, url=url))
        return refs

    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        """Walk the bean categories and yield every product exactly once.

        Args:
            fetcher: The shared polite fetcher.
            max_pages: How many categories to walk this run; None means all.

        Yields:
            One reference per product found.
        """
        cap = max_pages if max_pages is not None else self.max_pages
        seen: set[str] = set()
        for index in range(min(cap, len(CATEGORY_PATHS))):
            url = self.category_url(index)
            result = fetcher.get(url)
            fresh = [ref for ref in self.parse_listing(result.text) if ref.external_id not in seen]
            seen.update(ref.external_id for ref in fresh)
            yield from fresh

    def parse_product(self, html: str, ref: ProductRef) -> Coffee | None:
        """Turn one detail page into a coffee.

        Args:
            html: The detail page source.
            ref: What the listing page already told us about this product.

        Returns:
            The parsed coffee, or None for the capsules, packs and vouchers the
            bean categories list alongside whole beans.
        """
        soup = BeautifulSoup(html, "lxml")
        record = _page_data(soup)
        name = dom.text(soup.select_one("h1")) or _text_of(record.get("name")) or ref.name or ""
        if self.is_ignored(name):
            logger.debug("%s is not whole beans, skipping", name)
            return None
        raw_attributes, labels, intensities = _parse_params(soup)
        for key, value in dom.page_meta(soup).items():
            raw_attributes.setdefault(key, value)
        variants = _parse_variants(soup, ref)
        price = self._price(soup, record, ref)
        species = self._species(name, labels)
        coffee = Coffee(
            site=self.site_id,
            external_id=ref.external_id or external_id_of(ref.url) or "",
            url=ref.url,
            name=name,
            site_country=self.country,
            price=price,
            currency=DEFAULT_CURRENCY if price is not None else None,
            weight_g=variants[0].weight_g if variants else None,
            available=_parse_availability(soup, record),
            decaf="bezkofein" in normalize.fold(name) or "decaf" in normalize.fold(name),
            origin=self._origin(labels, name),
            processing=normalize.parse_processing(labels.get(_LABEL_PROCESS)),
            roast=self._roast(labels, ref),
            species=species,
            taste=self._taste(labels, intensities),
            variants=variants,
            images=_parse_images(soup),
            tags=dom.unique(dom.text(flag) for flag in soup.select("div.product-flags span.flag")),
            categories=self._categories(record),
            description=dom.text(soup.select_one("div.product-description")),
            origin_text=labels.get(_LABEL_ORIGIN),
            raw_attributes=raw_attributes,
        )
        self._apply_codes(coffee, record)
        return coffee

    # ------------------------------------------------------------------ pieces

    def _price(
        self,
        soup: BeautifulSoup,
        record: dict[str, object],
        ref: ProductRef,
    ) -> float | None:
        """Work out the shown price from the three places the shop states it.

        Args:
            soup: The parsed detail page.
            record: The ``page_data`` product record.
            ref: The reference being parsed.

        Returns:
            The price, or None when no source is readable.
        """
        from_record = _number(record.get("priceWithVat"))
        if from_record is not None:
            return from_record
        shown = normalize.parse_amount(dom.text(soup.select_one("p.price[data-price]")))
        return shown if shown is not None else ref.price

    def _species(self, name: str, labels: dict[str, str]) -> Species:
        """Read the arabica/robusta split, which the shop states only in prose.

        Args:
            name: The product name.
            labels: The folded parameter lookup.

        Returns:
            The composition; a single-origin lot is read as pure arabica.
        """
        blob = " ".join(labels.values())
        arabica, robusta = normalize.parse_species(blob)
        return Species(
            arabica_pct=arabica,
            robusta_pct=robusta,
            is_blend=normalize.detect_blend(name, arabica, robusta),
        )

    def _origin(self, labels: dict[str, str], name: str) -> Origin:
        """Read the origin block out of the parameter list.

        Args:
            labels: The folded parameter lookup.
            name: The product name, used when no origin label exists.

        Returns:
            The origin part of the model.
        """
        altitude_raw = labels.get(_LABEL_ALTITUDE)
        low, high = normalize.parse_altitude(altitude_raw)
        origin_text = labels.get(_LABEL_ORIGIN)
        return Origin(
            country=normalize.detect_country(origin_text) or normalize.detect_country(name),
            region=labels.get(_LABEL_REGION),
            farm=labels.get(_LABEL_LOCALITY),
            producer=labels.get(_LABEL_PRODUCER),
            washing_station=labels.get(_LABEL_STATION),
            altitude_min_m=low,
            altitude_max_m=high,
            altitude_raw=altitude_raw,
            variety=normalize.clean_variety(normalize.split_list(labels.get(_LABEL_VARIETY))),
            harvest=labels.get(_LABEL_HARVEST),
        )

    def _roast(self, labels: dict[str, str], ref: ProductRef) -> Roast:
        """Read the roast level and the brewing style the roast targets.

        Args:
            labels: The folded parameter lookup.
            ref: The reference, whose card names the profile the page omits.

        Returns:
            The roast part of the model.
        """
        raw = labels.get(_LABEL_ROAST)
        profile_text = " ".join(
            filter(None, (labels.get(_LABEL_BREWING), ref.extra.get("profile"), ref.url))
        )
        return Roast(
            level=normalize.normalize_roast_level(raw),
            raw=raw,
            profile=normalize.normalize_roast_profile(profile_text),
        )

    def _taste(self, labels: dict[str, str], intensities: dict[str, int]) -> Taste:
        """Read the bean bars, the cup notes and the brewing methods.

        Args:
            labels: The folded parameter lookup.
            intensities: The bean-bar counts, keyed by folded label.

        Returns:
            The sensory part of the model.
        """
        taste = Taste(
            flavor_notes=split_notes(labels.get(_LABEL_NOTES)),
            tasting_text=labels.get(_LABEL_TASTE),
            brewing_methods=normalize.split_list(labels.get(_LABEL_BREWING)),
            sca_score=normalize.parse_float(labels.get(_LABEL_SCA)),
        )
        for key, field in _INTENSITY_LABELS.items():
            value = intensities.get(key) or normalize.parse_intensity(labels.get(key))
            if value is not None:
                setattr(taste, field, value)
        return taste

    def _categories(self, record: dict[str, object]) -> list[str]:
        """Read the category trail out of the GA4 record.

        Args:
            record: The ``page_data`` product record.

        Returns:
            The category names, in breadcrumb order.
        """
        names: list[str | None] = []
        for key in ("categoryCurrent", "categoryMain"):
            entries = record.get(key)
            if not isinstance(entries, list):
                continue
            names.extend(
                _text_of(entry.get("name")) for entry in entries if isinstance(entry, dict)
            )
        return dom.unique(names)

    def _apply_codes(self, coffee: Coffee, record: dict[str, object]) -> None:
        """Keep the shop's own identifiers and campaign flags as raw attributes.

        Args:
            coffee: The coffee being filled in.
            record: The ``page_data`` product record.
        """
        for key in ("EAN", "code", "productCode", "variationName", "producer"):
            value = _text_of(record.get(key)) or (
                str(record[key]) if isinstance(record.get(key), int) else None
            )
            if value:
                coffee.raw_attributes.setdefault(key.upper(), value)
        campaigns = record.get("campaigns")
        if isinstance(campaigns, dict):
            names = dom.unique(
                _text_of(entry.get("name"))
                for entry in campaigns.values()
                if isinstance(entry, dict)
            )
            if names:
                coffee.raw_attributes.setdefault("CAMPAIGNS", ", ".join(names))
                coffee.tags = dom.unique([*coffee.tags, *names])
