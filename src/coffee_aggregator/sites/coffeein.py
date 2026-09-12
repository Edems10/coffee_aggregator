from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from coffee_aggregator import normalize
from coffee_aggregator.models import (
    DEFAULT_RATING_MAX,
    Coffee,
    Origin,
    Popularity,
    Review,
    Roast,
    RoastLevel,
    Species,
    Taste,
    Variant,
)
from coffee_aggregator.sites import html as dom
from coffee_aggregator.sites.base import DEFAULT_IGNORED, ProductRef, SiteAdapter
from coffee_aggregator.sites.registry import register

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from coffee_aggregator.http import PoliteFetcher

logger = logging.getLogger(__name__)

BASE_URL: Final = "https://www.coffeein.sk/"
CATEGORY_TEMPLATE: Final = "/kategoria/2/cerstvo-prazena-zrnkova-kava/{page}/"
SITEMAP_PATH: Final = "/sitemap.xml"
DEFAULT_CURRENCY: Final = "EUR"
#: Non-coffee products the shop lists in the same category (folded, diacritic-free).
IGNORED_MARKERS: Final = (
    *DEFAULT_IGNORED,
    "vratena",
    "nechcena",
    "darcekovy poukaz",
    "darcekova poukazka",
)

_DETAIL_ID_RE: Final = re.compile(r"/detail/(\d+)/")
_BACKGROUND_URL_RE: Final = re.compile(r"url\((?P<quote>['\"]?)(?P<url>[^)'\"]+)(?P=quote)\)")
_GTAG_ITEM_RE: Final = re.compile(r"\{[^{}]*?'item_id'\s*:\s*'(?P<id>\d+)'[^{}]*\}", re.DOTALL)
_GTAG_PRICE_RE: Final = re.compile(r"'price'\s*:\s*(?P<price>[\d.]+)")
_CART_ARGS_RE: Final = re.compile(r"add_to_cart\([^{]*\{(?P<fields>[^}]*)\}")
_CART_FIELD_RE: Final = re.compile(r"'(?P<key>\w+)'\s*:\s*'(?P<value>[^']*)'")
_SOLD_RE: Final = re.compile(r"vypite\s*:?\s*(?P<count>\d[\d\s]*)")
_SPECIES_LINE_RE: Final = re.compile(r"\d\s*%\s*(arabi|robus)", re.IGNORECASE)
_PACKAGE_RE: Final = re.compile(r"\(([^)]*)\)")

#: ``class`` token on ``div.gallery > span.roast_level`` → canonical roast level.
_ROAST_CLASS_LEVELS: Final = {
    "light": RoastLevel.LIGHT,
    "medium": RoastLevel.MEDIUM,
    "dark": RoastLevel.DARK,
}
#: Folded ``div.speci_param_name`` → the :class:`~coffee_aggregator.models.Taste` field.
_TASTE_BARS: Final = {
    "telo": "body",
    "horkost": "bitterness",
    "acidita": "acidity",
    "sladkost": "sweetness",
}
_LABEL_REGION: Final = "region"
_LABEL_FARM: Final = "farma"
_LABEL_FARMER: Final = "farmar"
_LABEL_PROCESSOR: Final = "spracovatel"
_LABEL_STATION: Final = "spracovatelska stanica"
_LABEL_ALTITUDE: Final = "nadmorska vyska"
_LABEL_VARIETY: Final = "odroda"
_LABEL_HARVEST: Final = "zber"
_LABEL_PROCESS: Final = "spracovanie"
_LABEL_ROAST: Final = "odtien prazenia"
_LABEL_BREWING: Final = "metoda pripravy"
_LABEL_PACKAGE: Final = "velkost balenia"


def _external_id(url: str | None) -> str | None:
    """Read the numeric product id out of a ``/detail/<id>/`` URL.

    Args:
        url: A product URL or href.

    Returns:
        The id as a string, or None when the URL is not a product URL.
    """
    if not url:
        return None
    match = _DETAIL_ID_RE.search(url)
    return match.group(1) if match else None


def _cart_fields(onsubmit: str | None) -> dict[str, str]:
    """Parse the inline ``add_to_cart(...)`` dictionary of a buy form.

    Args:
        onsubmit: The ``onsubmit`` attribute, when the form has one.

    Returns:
        The ``added_item_*`` fields; empty when the attribute is missing or the
        product name inside it contains a quote that breaks the literal.
    """
    if not onsubmit:
        return {}
    match = _CART_ARGS_RE.search(onsubmit)
    if match is None:
        return {}
    return {
        field.group("key"): field.group("value")
        for field in _CART_FIELD_RE.finditer(match.group("fields"))
    }


def _gtag_prices(soup: BeautifulSoup) -> dict[str, float]:
    """Read the GA4 ``view_item_list`` prices keyed by product id.

    Args:
        soup: The parsed listing page.

    Returns:
        A mapping of product id to price; empty when the script is absent.
    """
    prices: dict[str, float] = {}
    for script in soup.find_all("script"):
        body = script.get_text()
        if "view_item_list" not in body and "view_item" not in body:
            continue
        for item in _GTAG_ITEM_RE.finditer(body):
            price = _GTAG_PRICE_RE.search(item.group(0))
            if price is not None:
                prices.setdefault(item.group("id"), float(price.group("price")))
    return prices


def _gtag_item_id(soup: BeautifulSoup) -> str | None:
    """Read the product id out of the GA4 ``view_item`` event of a detail page.

    Args:
        soup: The parsed detail page.

    Returns:
        The first id found, or None.
    """
    for script in soup.find_all("script"):
        body = script.get_text()
        if "'view_item'" not in body:
            continue
        item = _GTAG_ITEM_RE.search(body)
        if item is not None:
            return item.group("id")
    return None


def _card_price(card: Tag, gtag: dict[str, float], external_id: str) -> float | None:
    """Work out a listing card's price from the three places the shop states it.

    Args:
        card: The ``li.product`` element.
        gtag: Prices read from the GA4 script.
        external_id: The product id of this card.

    Returns:
        The price, or None when no source is readable.
    """
    from_gtag = gtag.get(external_id)
    if from_gtag is not None:
        return from_gtag
    form = card.find("form")
    fields = _cart_fields(dom.attr(form, "onsubmit")) if isinstance(form, Tag) else {}
    price = normalize.parse_amount(fields.get("added_item_price"))
    if price is not None:
        return price
    return normalize.parse_amount(dom.text(card.select_one("form > span")))


def _card_extra(card: Tag) -> dict[str, str]:
    """Collect the listing-only strings of one product card.

    Args:
        card: The ``li.product`` element.

    Returns:
        Tags, flavour icons, stock text and roast badge as plain strings.
    """
    extra: dict[str, str] = {}
    tags = dom.unique(
        dom.text(tag) or " ".join(name for name in dom.classes(tag) if name != "tag")
        for tag in card.select("div.tags span.tag")
    )
    if tags:
        extra["tags"] = ", ".join(tags)
    flavours = dom.unique(dom.attr(img, "alt") for img in card.select("div.preparing img"))
    if flavours:
        extra["flavor_notes"] = ", ".join(flavours)
    stock = dom.text(card.select_one("span.stock"))
    if stock:
        extra["stock"] = stock
    roast = dom.text(card.select_one("span.roast_level"))
    if roast:
        extra["roast_level"] = roast
    stars = card.select("div.prod_rank span.rank_stars li.active")
    if stars:
        extra["rating"] = str(len(stars))
    return extra


def _card_image(card: Tag) -> str | None:
    """Read the thumbnail out of the card's inline ``background-image``.

    Args:
        card: The ``li.product`` element.

    Returns:
        The absolute image URL, or None.
    """
    style = dom.attr(card.select_one("div.prod_img"), "style")
    if not style:
        return None
    match = _BACKGROUND_URL_RE.search(style)
    return dom.absolute(BASE_URL, match.group("url")) if match else None


def _parse_labels(blocks: Iterable[Tag | None]) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Split the info blocks into labelled values and marketing prose.

    Args:
        blocks: The ``<br/>``-separated blocks to read, in priority order.

    Returns:
        A ``(raw_attributes, folded_lookup, prose)`` tuple, where
        ``raw_attributes`` keeps the labels as written (trimmed, upper-cased),
        ``folded_lookup`` is keyed by the folded label for field mapping, and
        ``prose`` holds every line that is not a ``LABEL: value`` pair.
    """
    parsed = dom.parse_label_lines(blocks)
    return parsed.raw(), parsed.folded(), parsed.prose


def _species_from_lines(lines: Iterable[str], name: str) -> Species:
    """Read the arabica/robusta split from the description lines.

    Parsing line by line (rather than over the whole block) is what keeps the
    ``ZBER: …, 2026`` line from bleeding into the ``100 % Arabika`` line.

    Args:
        lines: Every line of the description blocks.
        name: The product name, used to spot the word "zmes"/"blend".

    Returns:
        The composition; percentages stay None when the page never states them.
    """
    arabica: int | None = None
    robusta: int | None = None
    for line in lines:
        if _SPECIES_LINE_RE.search(line) is None:
            continue
        arabica, robusta = normalize.parse_species(line)
        if arabica is not None or robusta is not None:
            break
    return Species(
        arabica_pct=arabica,
        robusta_pct=robusta,
        is_blend=normalize.detect_blend(name, arabica, robusta),
    )


def _species_from_tags(soup: BeautifulSoup, name: str) -> Species:
    """Fall back to the ``<strong>`` tags when no description line states a split.

    Args:
        soup: The parsed detail page.
        name: The product name.

    Returns:
        The composition parsed from the emphasised text.
    """
    return _species_from_lines((dom.text(tag) or "" for tag in soup.select("strong")), name)


def _parse_variants(soup: BeautifulSoup, ref: ProductRef, price: float | None) -> list[Variant]:
    """Read the package-size switcher, whose options are sibling product pages.

    Args:
        soup: The parsed detail page.
        ref: The reference of the page being parsed.
        price: The price of the current package, for the selected option.

    Returns:
        One variant per option, the current package included.
    """
    variants: list[Variant] = []
    for option in soup.select("div.other_weights select#other_weight option"):
        label = dom.text(option)
        url = dom.absolute(BASE_URL, dom.attr(option, "value"))
        is_current = url is None
        variants.append(
            Variant(
                external_id=_external_id(url) if url else ref.external_id or None,
                url=url or ref.url,
                weight_g=normalize.parse_weight_grams(label),
                price=price if is_current else None,
                currency=DEFAULT_CURRENCY if is_current and price is not None else None,
                label=label,
            )
        )
    return variants


def _parse_taste(soup: BeautifulSoup, brewing: str | None) -> Taste:
    """Read the 0-5 characteristic bars, the flavour icons and the tasting prose.

    Args:
        soup: The parsed detail page.
        brewing: The ``Metóda prípravy`` value, when the page states one.

    Returns:
        The sensory part of the model.
    """
    taste = Taste()
    for bar in soup.select("div.speci_param"):
        field = _TASTE_BARS.get(normalize.fold(dom.text(bar.select_one("div.speci_param_name"))))
        if field is None:
            continue
        setattr(taste, field, len(bar.select("span.point_full")))
    taste.flavor_notes = dom.unique(
        dom.text(span) for span in soup.select("div.recommended_preparation span")
    )
    taste.tasting_text = dom.text(soup.select_one("div#coffee_taste"))
    taste.brewing_methods = normalize.split_list(brewing) or dom.unique(
        dom.text(span) for span in soup.select("div.recipes div.recipe span")
    )
    return taste


def _parse_roast(soup: BeautifulSoup, raw: str | None) -> Roast:
    """Read the roast badge, the roast shade label and the freshness dates.

    Args:
        soup: The parsed detail page.
        raw: The ``Odtieň praženia`` value, when the page states one.

    Returns:
        The roast part of the model.
    """
    badge = soup.select_one("div.gallery > span.roast_level")
    badge_text = dom.text(badge)
    level = normalize.normalize_roast_level(raw)
    if level is RoastLevel.UNKNOWN:
        from_class = (
            _ROAST_CLASS_LEVELS[name] for name in dom.classes(badge) if name in _ROAST_CLASS_LEVELS
        )
        level = next(from_class, normalize.normalize_roast_level(badge_text))
    preparation = dom.text(soup.select_one("div.popis_date_data div.kategoria_espresso"))
    profile = normalize.normalize_roast_profile(" ".join(filter(None, (badge_text, preparation))))
    roast = Roast(level=level, raw=raw or badge_text, profile=profile)
    for block in soup.select("div.popis_date_data div.praz_date"):
        text = dom.text(block) or ""
        folded = normalize.fold(text)
        if folded.startswith("prazenie"):
            roast.roast_date = normalize.parse_date_dmy(text)
        elif "trvanlivost" in folded:
            roast.best_before = normalize.parse_date_dmy(text)
    return roast


def _parse_reviews(soup: BeautifulSoup) -> list[Review]:
    """Read every published customer review with its author, date and stars.

    Args:
        soup: The parsed detail page.

    Returns:
        One :class:`~coffee_aggregator.models.Review` per review block.
    """
    reviews: list[Review] = []
    for item in soup.select("#ranks_box li[itemprop=review]"):
        rating = dom.attr(
            item.select_one("[itemprop=reviewRating] [itemprop=ratingValue]"), "content"
        )
        published = item.select_one("[itemprop=datePublished]")
        reviews.append(
            Review(
                author=dom.text(item.select_one("[itemprop=author] [itemprop=name]")),
                date=normalize.parse_date_dmy(
                    dom.attr(published, "content") or dom.text(published)
                ),
                rating=normalize.parse_float(rating),
                text=dom.text(item.select_one("div.rank_right")),
            )
        )
    return reviews


def _parse_popularity(soup: BeautifulSoup) -> Popularity:
    """Read the aggregate rating, the review list and the lifetime sales counter.

    Args:
        soup: The parsed detail page.

    Returns:
        The social-proof part of the model.
    """
    rankbox = soup.select_one("div.rankbox")
    rating_tag = rankbox.select_one("[itemprop=ratingValue]") if rankbox else None
    count_tag = rankbox.select_one("[itemprop=reviewCount]") if rankbox else None
    best = dom.attr(rankbox.select_one("[itemprop=bestRating]"), "content") if rankbox else None
    sold = _SOLD_RE.search(normalize.fold(dom.text(soup.select_one("div.popis_date_data"))))
    return Popularity(
        rating=normalize.parse_float(dom.attr(rating_tag, "content") or dom.text(rating_tag)),
        rating_max=normalize.parse_int(best) or DEFAULT_RATING_MAX,
        review_count=normalize.parse_int(dom.attr(count_tag, "content") or dom.text(count_tag)),
        reviews=_parse_reviews(soup),
        sold_count=normalize.parse_int(sold.group("count")) if sold else None,
    )


def _parse_availability(soup: BeautifulSoup) -> bool | None:
    """Decide whether the product is in stock.

    Args:
        soup: The parsed detail page.

    Returns:
        True, False, or None when the page says nothing.
    """
    schema = normalize.fold(dom.attr(soup.select_one("meta[itemprop=availability]"), "content"))
    if "instock" in schema.replace(" ", ""):
        return True
    if schema:
        return False
    stock = normalize.fold(dom.text(soup.select_one("div.popis_date_data div.dost")))
    if "skladom" in stock:
        return True
    if "vypredane" in stock or "nedostupne" in stock:
        return False
    return None


def _parse_images(soup: BeautifulSoup) -> list[str]:
    """Collect the product photos, skipping the award badges.

    Args:
        soup: The parsed detail page.

    Returns:
        Absolute image URLs in page order.
    """
    candidates = [
        dom.attr(soup.select_one("div.gallery a.gallery_first_img"), "href"),
        dom.attr(soup.select_one("div.gallery img[itemprop=image]"), "src"),
        *(dom.attr(link, "href") for link in soup.select("div.gallery div.images a")),
    ]
    return dom.unique(
        dom.absolute(BASE_URL, url) for url in candidates if url and "/images/gta/" not in url
    )


def _parse_awards(soup: BeautifulSoup) -> list[str]:
    """Read the award badges pinned onto the main product photo.

    Args:
        soup: The parsed detail page.

    Returns:
        The badge names, taken from the alt text or the image basename.
    """
    awards: list[str | None] = []
    for image in soup.select("div.gallery img.taste_awards"):
        src = dom.attr(image, "src") or ""
        basename = src.rsplit("/", 1)[-1].rsplit(".", 1)[0] or None
        awards.append(dom.attr(image, "alt") or basename)
    return dom.unique(awards)


def _parse_categories(soup: BeautifulSoup) -> list[str]:
    """Read the breadcrumb trail and the preparation category link.

    Args:
        soup: The parsed detail page.

    Returns:
        The category names, without the shop's home link.
    """
    breadcrumbs = [
        dom.text(link)
        for link in soup.select("div.breadcrumbs a")
        if (dom.attr(link, "href") or "/") != "/"
    ]
    preparation = [dom.text(link) for link in soup.select("div.kategoria_espresso a")]
    return dom.unique([*breadcrumbs, *preparation])


def _category_hrefs(soup: BeautifulSoup) -> str:
    """Join the product's own category hrefs so category ids can be tested for.

    Only the breadcrumb trail and the preparation link belong to the product —
    the shop's left-hand menu links every category from every page.

    Args:
        soup: The parsed detail page.

    Returns:
        One space-separated string of hrefs.
    """
    selector = "div.breadcrumbs a[href], div.kategoria_espresso a[href]"
    return " ".join(dom.attr(link, "href") or "" for link in soup.select(selector))


def _detect_country(name: str, soup: BeautifulSoup, origin_text: str | None, *, blend: bool) -> str:
    """Work out the country of origin, which the shop never states as a field.

    Args:
        name: The product name, by far the most reliable source.
        soup: The parsed detail page, for the keyword meta and breadcrumbs.
        origin_text: The "Pôvod kávy" prose.
        blend: Whether the product is a blend — a blend's origin prose names the
            countries of its components, so it must not set a single country.

    Returns:
        An ISO alpha-2 code, or an empty string when nothing is recognised.
    """
    sources = [name, dom.attr(soup.select_one("meta[name=keywords]"), "content")]
    if not blend:
        sources.extend([" ".join(_parse_categories(soup)), origin_text])
    for source in sources:
        country = normalize.detect_country(source)
        if country is not None:
            return country
    return ""


@register
class CoffeeinSite(SiteAdapter):
    """coffeein.sk — a Slovak roastery with a bespoke, server-rendered shop."""

    site_id = "coffeein"
    name = "Coffeein"
    country = "SK"
    base_url = BASE_URL
    kind = "bespoke"
    max_pages = 20

    def __init__(self, *, use_sitemap: bool = False) -> None:
        """Build the adapter.

        Args:
            use_sitemap: Enumerate every product from ``/sitemap.xml`` instead of
                walking the coffee-bean category. The sitemap lists the whole
                catalogue (mugs, filters, cascara), so the category walk stays
                the default.
        """
        self.use_sitemap = use_sitemap

    def ignored_names(self) -> tuple[str, ...]:
        """Return the folded markers of products that are not coffee beans.

        Returns:
            The default markers plus the shop's returned-coffee lots.
        """
        return IGNORED_MARKERS

    def category_url(self, page: int) -> str:
        """Return the URL of one page of the coffee-bean category.

        Args:
            page: The 1-based page number.

        Returns:
            The absolute listing URL.
        """
        return urljoin(BASE_URL, CATEGORY_TEMPLATE.format(page=page))

    def parse_listing(self, html: str) -> list[ProductRef]:
        """Turn one listing page into product references.

        Every reference carries the real ``a.headline`` href — the slug is never
        rebuilt from the name, so quotes and apostrophes in names are harmless.

        Args:
            html: The listing page source.

        Returns:
            One reference per product card, in page order.
        """
        soup = BeautifulSoup(html, "lxml")
        gtag = _gtag_prices(soup)
        refs: list[ProductRef] = []
        for card in soup.select("ul.product_list li.product"):
            link = card.select_one("a.headline")
            url = dom.absolute(BASE_URL, dom.attr(link, "href"))
            external_id = _external_id(url)
            if url is None or external_id is None:
                logger.debug("skipping a product card without a /detail/ link")
                continue
            price = _card_price(card, gtag, external_id)
            refs.append(
                ProductRef(
                    site_id=self.site_id,
                    external_id=external_id,
                    url=url,
                    name=dom.text(link),
                    price=price,
                    currency=DEFAULT_CURRENCY if price is not None else None,
                    image_url=_card_image(card),
                    extra=_card_extra(card),
                )
            )
        return refs

    def parse_sitemap(self, xml: str) -> list[ProductRef]:
        """Turn the sitemap into product references.

        Args:
            xml: The ``sitemap.xml`` body.

        Returns:
            One reference per ``/detail/<id>/`` location.
        """
        soup = BeautifulSoup(xml, "xml")
        refs: list[ProductRef] = []
        seen: set[str] = set()
        for location in soup.find_all("loc"):
            url = location.get_text(strip=True)
            external_id = _external_id(url)
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
        """Walk the coffee-bean category (or the sitemap) and yield every product.

        Args:
            fetcher: The shared polite fetcher.
            max_pages: Cap on listing pages for this run only.

        Yields:
            One reference per product found.
        """
        if self.use_sitemap:
            yield from self._discover_sitemap(fetcher)
            return
        cap = max_pages if max_pages is not None else self.max_pages
        seen: set[str] = set()
        for page in range(1, cap + 1):
            url = self.category_url(page)
            result = fetcher.get(url)
            if result.final_url.rstrip("/") != url.rstrip("/"):
                logger.debug("listing page %d redirected to %s, stopping", page, result.final_url)
                return
            fresh = [ref for ref in self.parse_listing(result.text) if ref.external_id not in seen]
            if not fresh:
                logger.debug("listing page %d added no new products, stopping", page)
                return
            seen.update(ref.external_id for ref in fresh)
            yield from fresh

    def _discover_sitemap(self, fetcher: PoliteFetcher) -> Iterator[ProductRef]:
        result = fetcher.get(urljoin(BASE_URL, SITEMAP_PATH))
        yield from self.parse_sitemap(result.text)

    def parse_product(self, html: str, ref: ProductRef) -> Coffee | None:
        """Turn one detail page into a coffee.

        Args:
            html: The detail page source.
            ref: What the listing page already told us about this product.

        Returns:
            The parsed coffee, or None for the non-coffee items the shop lists in
            the same category (cascara, returned lots, tasting packs).
        """
        soup = BeautifulSoup(html, "lxml")
        name = dom.text(soup.select_one("h1[itemprop=name]")) or ref.name or ""
        if self.is_ignored(name):
            logger.debug("%s is not coffee beans, skipping", name)
            return None
        description_block = soup.select_one("p[itemprop=description]")
        raw_attributes, labels, prose = _parse_labels(
            (description_block, soup.select_one("div.long_desc_desc"))
        )
        # Open Graph and <meta> often name the origin the visible markup omits;
        # a real label always wins, so this only ever adds.
        for key, value in dom.page_meta(soup).items():
            raw_attributes.setdefault(key, value)
        price, currency = self._parse_price(soup, ref)
        species = _species_from_lines([*prose, *labels.values()], name)
        if species.arabica_pct is None and species.robusta_pct is None:
            species = _species_from_tags(soup, name)
        origin_text = dom.text(soup.select_one("div#coffee_origin"))
        coffee = Coffee(
            site=self.site_id,
            external_id=ref.external_id or _gtag_item_id(soup) or "",
            url=ref.url,
            name=name,
            site_country=self.country,
            price=price,
            currency=currency,
            weight_g=self._parse_weight(name, labels),
            available=_parse_availability(soup),
            origin=self._parse_origin(soup, labels, name, origin_text, blend=species.is_blend),
            processing=normalize.parse_processing(labels.get(_LABEL_PROCESS)),
            roast=_parse_roast(soup, labels.get(_LABEL_ROAST)),
            species=species,
            taste=_parse_taste(soup, labels.get(_LABEL_BREWING)),
            popularity=_parse_popularity(soup),
            variants=_parse_variants(soup, ref, price),
            images=_parse_images(soup),
            tags=dom.unique(dom.text(tag) for tag in soup.select("div.popis div.tags span.tag")),
            categories=_parse_categories(soup),
            awards=_parse_awards(soup),
            description="\n".join(prose) or None,
            origin_text=origin_text,
            raw_attributes=raw_attributes,
        )
        self._apply_flags(coffee, soup, prose)
        self._apply_discount(coffee, soup)
        return coffee

    def _parse_price(self, soup: BeautifulSoup, ref: ProductRef) -> tuple[float | None, str | None]:
        price_tag = soup.select_one("span.product_price")
        price = normalize.parse_amount(dom.attr(price_tag, "content"))
        if price is None:
            price = normalize.parse_amount(dom.text(price_tag)) or ref.price
        currency = dom.attr(soup.select_one("[itemprop=priceCurrency]"), "content")
        currency = currency or normalize.detect_currency(dom.text(price_tag)) or ref.currency
        return price, currency if price is not None else None

    def _parse_weight(self, name: str, labels: dict[str, str]) -> int | None:
        for package in _PACKAGE_RE.findall(name):
            weight = normalize.parse_weight_grams(package)
            if weight is not None:
                return weight
        return normalize.parse_weight_grams(labels.get(_LABEL_PACKAGE))

    def _parse_origin(
        self,
        soup: BeautifulSoup,
        labels: dict[str, str],
        name: str,
        origin_text: str | None,
        *,
        blend: bool,
    ) -> Origin:
        altitude_raw = labels.get(_LABEL_ALTITUDE)
        low, high = normalize.parse_altitude(altitude_raw)
        return Origin(
            country=_detect_country(name, soup, origin_text, blend=blend) or None,
            region=labels.get(_LABEL_REGION),
            farm=labels.get(_LABEL_FARM),
            producer=labels.get(_LABEL_FARMER) or labels.get(_LABEL_PROCESSOR),
            washing_station=labels.get(_LABEL_STATION),
            altitude_min_m=low,
            altitude_max_m=high,
            altitude_raw=altitude_raw,
            variety=normalize.clean_variety(normalize.split_list(labels.get(_LABEL_VARIETY))),
            harvest=labels.get(_LABEL_HARVEST),
        )

    def _apply_flags(self, coffee: Coffee, soup: BeautifulSoup, prose: list[str]) -> None:
        hrefs = _category_hrefs(soup)
        blob = normalize.fold(" ".join([coffee.name, *prose]))
        coffee.decaf = "bezkofein" in blob or "/kategoria/92/" in hrefs
        coffee.specialty_grade = "specialty grade" in blob or "/kategoria/55/" in hrefs

    def _apply_discount(self, coffee: Coffee, soup: BeautifulSoup) -> None:
        """Record the shop's only discount signal, the inline add-to-cart dict.

        ``added_item_discount`` is an absolute amount off the shown price, so the
        pre-discount price is the sum of the two.

        Args:
            coffee: The coffee being filled in.
            soup: The parsed detail page.
        """
        form = soup.select_one("div.price_fixed_box form[action^='/kosik/']")
        fields = _cart_fields(dom.attr(form, "onsubmit"))
        for key, value in fields.items():
            coffee.raw_attributes.setdefault(key.upper(), value)
        discount = normalize.parse_amount(fields.get("added_item_discount"))
        if discount and coffee.price is not None:
            coffee.original_price = round(coffee.price + discount, 2)
