from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from coffee_aggregator import normalize
from coffee_aggregator.labels import (
    F_ACIDITY,
    F_BITTERNESS,
    F_BODY,
    F_BREWING,
    F_COUNTRY,
    F_FARM,
    F_PROCESS,
    F_SWEETNESS,
    bar,
    headline_weight,
    is_decaf,
    map_label,
    parse_origin,
    parse_roast,
    score,
)
from coffee_aggregator.models import Coffee, Species, Taste, Variant
from coffee_aggregator.sites import html as dom
from coffee_aggregator.sites import toolkit as kit
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

PRODUCT_ID_RE: Final = re.compile(r"_z(?P<id>\d+)/?(?:[?#]|$)")
#: ``page_data = {...};page_data[`` — the detail page's own GA4 product record.
_PAGE_DATA_RE: Final = re.compile(r"page_data\s*=\s*(?P<json>\{.*?\})\s*;\s*page_data\[", re.DOTALL)
#: ``gtm_prva[1417] = {'id': ..., 'price': ...};`` — one entry per package size.
_VARIATION_RE: Final = re.compile(r"gtm_prva\[(?P<id>\d+)\]\s*=\s*\{(?P<fields>[^}]*)\}")
_VARIATION_FIELD_RE: Final = re.compile(r"'(?P<key>\w+)'\s*:\s*'?(?P<value>[^',}]*)'?")
#: The shop writes its cup notes as ``a - b - c``, which the shared splitter
#: leaves whole because a bare dash also joins words.
_NOTE_SPLIT_RE: Final = re.compile(r"\s+[‐-―−-]\s+")

#: The shop's own spellings, over the shared vocabulary. It declines its sensory
#: rows ("intenzita těla", never the bare "tělo" the vocabulary knows) and reads
#: "Lokalita" as the place the lot comes from rather than as a wider region.
SHOP_TERMS: Final[dict[str, str]] = {
    "intenzita tela": F_BODY,
    "intenzita kyselosti": F_ACIDITY,
    "intenzita horkosti": F_BITTERNESS,
    "intenzita sladkosti": F_SWEETNESS,
    "lokalita": F_FARM,
}
LABEL_MAP: Final[dict[str, str]] = kit.vocabulary(SHOP_TERMS)

#: The two flavour rows the shop prints side by side. One canonical flavour
#: field cannot hold both, so they are read by their own spellings: "Chuť" is a
#: one-word verdict and "Charakteristika" is the list of cup notes.
_LABEL_TASTE: Final = "chut"
_LABEL_NOTES: Final = "charakteristika"

_BARS: Final = (
    (F_BODY, "body"),
    (F_ACIDITY, "acidity"),
    (F_BITTERNESS, "bitterness"),
    (F_SWEETNESS, "sweetness"),
)


def external_id_of(url: str | None) -> str | None:
    """Read the numeric product id out of a ``..._z<id>/`` URL.

    Args:
        url: A product URL or href.

    Returns:
        The id as a string, or None when the URL is not a product URL.
    """
    return kit.id_from(url, PRODUCT_ID_RE)


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


def _tracking(tag: Tag | None) -> dict[str, object]:
    """Decode the GA4 record the shop hangs off every product link.

    Args:
        tag: The ``a.product-link`` element.

    Returns:
        The first product record, or an empty mapping.
    """
    decoded = kit.json_object(dom.attr(tag, "data-tracking-click"), what="tracking data")
    click = decoded.get("click")
    return kit.first_record(click if isinstance(click, dict) else decoded, "products")


def _page_data(soup: BeautifulSoup) -> dict[str, object]:
    """Read the ``page_data`` record a detail page pushes into the data layer.

    Args:
        soup: The parsed detail page.

    Returns:
        The first product record, or an empty mapping.
    """
    for script in soup.find_all("script"):
        decoded = kit.embedded_json(script.get_text(), _PAGE_DATA_RE, what="page_data script")
        record = kit.first_record(decoded, "products")
        if record:
            return record
    return {}


def _read_params(soup: BeautifulSoup) -> tuple[kit.Facts, dict[str, int]]:
    """Read the ``Detaily`` parameter list, bean bars included.

    Each row is ``<li><strong>LABEL</strong><span>VALUE</span></li>``; an
    intensity row instead draws five bean icons, of which the active ones are
    the value on the shop's own 1-5 scale.

    Args:
        soup: The parsed detail page.

    Returns:
        The labelled facts, and the bean-bar counts keyed by canonical field.
    """
    facts = kit.Facts(label_map=LABEL_MAP)
    intensities: dict[str, int] = {}
    for row in soup.select("div.product-params li"):
        label = dom.text(row.select_one("strong"))
        if not label:
            continue
        drawn = row.select_one("p.intensity")
        # The shop glues its units on with a no-break space; dash_fold_keep turns
        # that into a plain one without touching case or diacritics.
        source = dom.text(drawn.select_one("span")) if drawn else dom.text(row.select_one("span"))
        value = normalize.dash_fold_keep(source) or None
        active = drawn.select("span.icons_bean:not(.inactive)") if drawn else []
        field_name = map_label(normalize.fold(label), LABEL_MAP)
        if active and field_name:
            intensities[field_name] = len(active)
        facts.add(label, value)
    return facts, intensities


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
                found.group("key"): found.group("value").strip()
                for found in _VARIATION_FIELD_RE.finditer(match.group("fields"))
            }
            variants.append(
                kit.package(
                    external_id=fields.get("id") or identifier,
                    url=f"{ref.url}#{identifier}",
                    label=fields.get("variationName"),
                    price=normalize.parse_amount(fields.get("price")),
                    currency=DEFAULT_CURRENCY,
                )
            )
    return variants


def _availability(soup: BeautifulSoup, record: dict[str, object]) -> bool | None:
    """Decide whether the product is in stock.

    Args:
        soup: The parsed detail page.
        record: The ``page_data`` product record.

    Returns:
        True, False, or None when neither source says.
    """
    stated = kit.stock_state(
        kit.as_str(record.get("availability")),
        dom.text(soup.select_one("[data-deliverytime]")),
    )
    if stated is not None:
        return stated
    sold_out = kit.as_number(record.get("soldOut"))
    return False if sold_out is not None and sold_out > 0 else None


def _parse_images(soup: BeautifulSoup) -> list[str]:
    """Collect the product photos from the gallery and the Open Graph tag.

    Args:
        soup: The parsed detail page.

    Returns:
        Absolute image URLs, in page order.
    """
    return kit.gallery(
        BASE_URL,
        [
            *(dom.attr(link, "href") for link in soup.select("div.product-gallery a[href]")),
            *(dom.attr(image, "src") for image in soup.select("div.product-gallery img[src]")),
            dom.attr(soup.select_one('meta[property="og:image"]'), "content"),
        ],
        # Everything the shop serves from another path is a layout asset.
        keep_when=lambda url: "/data/tmp/" in url,
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
    for key, source in (("stock", "availability"), ("producer", "producer")):
        value = kit.as_str(record.get(source))
        if value:
            extra[key] = value
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
            card = link.find_parent("div", class_="catalog")
            refs.append(
                kit.product_ref(
                    self.site_id,
                    external_id,
                    url,
                    name=dom.text(link.select_one("h3.title")) or kit.as_str(record.get("name")),
                    price=kit.as_number(record.get("priceWithVat")),
                    currency=DEFAULT_CURRENCY,
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
        return kit.sitemap_refs(xml, self.site_id, PRODUCT_ID_RE)

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
        urls = [self.category_url(index) for index in range(min(cap, len(CATEGORY_PATHS)))]
        # The three categories are distinct lists, not pages of one: a category
        # whose beans all appeared in the previous one is not the end of the
        # catalogue, so only a redirect stops the walk.
        yield from kit.walk_listing(fetcher, urls, self.parse_listing, stop_when_stale=False)

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
        name = dom.text(soup.select_one("h1")) or kit.as_str(record.get("name")) or ref.name or ""
        if self.is_ignored(name):
            logger.debug("%s is not whole beans, skipping", name)
            return None
        facts, intensities = _read_params(soup)
        kit.keep(facts.raw, dom.page_meta(soup))
        variants = _parse_variants(soup, ref)
        price = self._price(soup, record, ref)
        species = self._species(name, facts)
        categories = self._categories(record)
        coffee = Coffee(
            site=self.site_id,
            external_id=ref.external_id or external_id_of(ref.url) or "",
            url=ref.url,
            name=name,
            site_country=self.country,
            price=price,
            currency=DEFAULT_CURRENCY if price is not None else None,
            weight_g=headline_weight(facts.labels, name, variants, price=price),
            available=_availability(soup, record),
            decaf=is_decaf(facts.labels, name, categories),
            origin=parse_origin(facts.labels, name, blend=species.is_blend),
            processing=normalize.parse_processing(facts.get(F_PROCESS)),
            roast=parse_roast(facts.labels, [*categories, ref.extra.get("profile", ""), ref.url]),
            species=species,
            taste=self._taste(facts, intensities),
            variants=variants,
            images=_parse_images(soup),
            tags=dom.unique(dom.text(flag) for flag in soup.select("div.product-flags span.flag")),
            categories=categories,
            description=dom.text(soup.select_one("div.product-description")),
            origin_text=facts.get(F_COUNTRY),
            raw_attributes=facts.raw,
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
        from_record = kit.as_number(record.get("priceWithVat"))
        if from_record is not None:
            return from_record
        shown = normalize.parse_amount(dom.text(soup.select_one("p.price[data-price]")))
        return shown if shown is not None else ref.price

    def _species(self, name: str, facts: kit.Facts) -> Species:
        """Read the arabica/robusta split, which the shop states only in prose.

        Args:
            name: The product name.
            facts: The labelled values of the page.

        Returns:
            The composition; the split is hunted across every parameter value
            because the shop names no parameter for it.
        """
        arabica, robusta = normalize.parse_species(" ".join(facts.values()))
        return Species(
            arabica_pct=arabica,
            robusta_pct=robusta,
            is_blend=normalize.detect_blend(name, arabica, robusta),
        )

    def _taste(self, facts: kit.Facts, intensities: dict[str, int]) -> Taste:
        """Read the bean bars, the cup notes and the brewing methods.

        The shop prints two flavour rows: "Chuť" is a one-word verdict and
        "Charakteristika" the list of cup notes, so the two are read apart.

        Args:
            facts: The labelled values of the page.
            intensities: The bean-bar counts, keyed by canonical field.

        Returns:
            The sensory part of the model.
        """
        taste = Taste(
            flavor_notes=split_notes(facts.pick(_LABEL_NOTES)),
            tasting_text=facts.pick(_LABEL_TASTE),
            brewing_methods=normalize.split_list(facts.get(F_BREWING)),
            sca_score=score(facts.labels),
        )
        for field_name, attribute in _BARS:
            value = intensities.get(field_name) or bar(facts.labels, field_name)
            if value is not None:
                setattr(taste, attribute, value)
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
            names.extend(
                kit.as_str(kit.as_dict(entry).get("name")) for entry in kit.as_list(record.get(key))
            )
        return dom.unique(names)

    def _apply_codes(self, coffee: Coffee, record: dict[str, object]) -> None:
        """Keep the shop's own identifiers and campaign flags as raw attributes.

        Args:
            coffee: The coffee being filled in.
            record: The ``page_data`` product record.
        """
        kit.keep(
            coffee.raw_attributes,
            kit.strings(record, ("EAN", "code", "productCode", "variationName", "producer")),
        )
        campaigns = kit.as_dict(record.get("campaigns"))
        names = dom.unique(
            kit.as_str(kit.as_dict(entry).get("name")) for entry in campaigns.values()
        )
        if names:
            coffee.raw_attributes.setdefault("CAMPAIGNS", ", ".join(names))
            coffee.tags = dom.unique([*coffee.tags, *names])
