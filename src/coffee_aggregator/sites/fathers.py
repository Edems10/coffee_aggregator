from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin

from coffee_aggregator import normalize
from coffee_aggregator.models import (
    Coffee,
    Origin,
    Roast,
    RoastProfile,
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

BASE_URL: Final = "https://fathers.cz/"
#: One page carries the whole catalogue, so the shop costs exactly one request.
LISTING_PATH: Final = "/kava"
DEFAULT_CURRENCY: Final = "CZK"
#: The shop quotes one price per VAT rate; this is the one a Czech buyer pays.
DEFAULT_DESTINATION: Final = "Czechia"
LANGUAGE: Final = "cs"
#: ``photo.bucket`` -> CDN host, as the page's ``#global-settings`` script states.
FILE_BUCKETS: Final = {
    "Storage": "https://storage.fathers.cz",
    "Upload": "https://upload.fathers.cz",
}
#: Subcategory slugs that hold coffee rather than merchandise or hardware.
COFFEE_SUBCATEGORIES: Final = ("espresso", "filtr", "kapsle", "dripbagy")
#: Folded markers of products that are coffee but not whole beans.
IGNORED_MARKERS: Final = (
    *DEFAULT_IGNORED,
    "sample box",
    "tasting box",
    "drip bag",
    "dripbag",
    "kapsle",
)

#: The SvelteKit page embeds its entire server payload in this one script tag.
_PROPS_RE: Final = re.compile(
    r'<script id="props"[^>]*>\s*(?P<json>\{.*?\})\s*</script>',
    re.DOTALL,
)
#: A ``LABEL: value`` line whose label is really a bracketed URL, not a label.
_URL_LABEL_RE: Final = re.compile(r"https?|^\W*\[")

_LABEL_COUNTRY: Final = ("zeme",)
_LABEL_REGION: Final = ("oblast", "region")
_LABEL_FARM: Final = ("farma stanice", "farma", "stanice")
_LABEL_PRODUCER: Final = ("producent",)
_LABEL_ALTITUDE: Final = ("nadmorska vyska", "nad. vyska", "nadm. vyska", "vyska")
_LABEL_VARIETY: Final = ("odruda",)
_LABEL_PROCESS: Final = ("zpracovani",)
_LABEL_NOTES: Final = ("chutovy profil", "chut", "chutove tony")
_LABEL_TYPE: Final = ("typ kavy",)
_LABEL_HARVEST: Final = ("sklizen", "zber")

#: Folded subcategory slug -> the roast profile it stands for.
_PROFILE_BY_SUBCATEGORY: Final = {
    "espresso": RoastProfile.ESPRESSO,
    "filtr": RoastProfile.FILTER,
    "dripbagy": RoastProfile.FILTER,
}


def _as_dict(value: object) -> dict[str, object]:
    """Narrow a decoded JSON value to a mapping.

    Args:
        value: Anything ``json.loads`` produced.

    Returns:
        The mapping, or an empty one when the value is of another shape.
    """
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    """Narrow a decoded JSON value to a list.

    Args:
        value: Anything ``json.loads`` produced.

    Returns:
        The list, or an empty one when the value is of another shape.
    """
    return value if isinstance(value, list) else []


def _as_str(value: object) -> str | None:
    """Narrow a decoded JSON value to a non-empty string.

    Args:
        value: Anything ``json.loads`` produced.

    Returns:
        The trimmed string, or None when it is absent or empty.
    """
    return value.strip() or None if isinstance(value, str) else None


def _as_number(value: object) -> float | None:
    """Narrow a decoded JSON value to a number.

    Args:
        value: Anything ``json.loads`` produced.

    Returns:
        The number, or None when the value is not numeric. Booleans are
        rejected: ``True`` is not a price.
    """
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _localised(value: object, language: str = LANGUAGE) -> str | None:
    """Read one language out of the shop's ``{"cs": ..., "en": ...}`` fields.

    Args:
        value: The localised field, which may also be a plain string.
        language: The language to prefer.

    Returns:
        The text in the wanted language, falling back to any other language.
    """
    if isinstance(value, str):
        return value.strip() or None
    mapping = _as_dict(value)
    wanted = _as_str(mapping.get(language))
    if wanted is not None:
        return wanted
    return next((text for text in (_as_str(item) for item in mapping.values()) if text), None)


def parse_props(text: str) -> dict[str, object]:
    """Read the ``#props`` payload the storefront embeds in every page.

    Args:
        text: A page source, or the JSON document a :class:`ProductRef` carries.

    Returns:
        The decoded payload; empty when the text holds neither.
    """
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            return _as_dict(json.loads(stripped))
        except json.JSONDecodeError:
            logger.debug("a product payload was not valid JSON")
            return {}
    match = _PROPS_RE.search(text)
    if match is None:
        return {}
    try:
        return _as_dict(json.loads(match.group("json")))
    except json.JSONDecodeError:
        logger.debug("the #props script was not valid JSON")
        return {}


def _price_for(
    prices: object,
    currency: str,
    destination: str,
) -> tuple[float | None, float | None]:
    """Pick the price a buyer in one country pays, out of the VAT-rate matrix.

    Args:
        prices: The variant's ``prices`` mapping, keyed by currency then rate.
        currency: The currency to read.
        destination: The country whose VAT rate applies.

    Returns:
        A ``(with_tax, without_tax)`` tuple; both None when nothing matches.
    """
    by_rate = _as_dict(_as_dict(prices).get(currency))
    fallback: tuple[float | None, float | None] = (None, None)
    for entry in by_rate.values():
        record = _as_dict(entry)
        price = _as_dict(record.get("price"))
        pair = (_as_number(price.get("withTax")), _as_number(price.get("withoutTax")))
        countries = [_as_str(item) for item in _as_list(record.get("countries"))]
        if destination in countries:
            return pair
        if fallback == (None, None):
            fallback = pair
    return fallback


def _stock_available(stock: list[object], variant_id: str | None) -> bool | None:
    """Decide whether one variant is in stock.

    Coffee is roasted to order and carries no stock row at all; only physical
    goods do. A row that names the variant is therefore authoritative, and its
    absence means "not stock-tracked", not "sold out".

    Args:
        stock: The product's ``stock`` rows.
        variant_id: The variant to look up.

    Returns:
        True, False, or None when the variant is unknown.
    """
    if variant_id is None:
        return None
    amounts = [
        _as_number(row.get("amount"))
        for row in (_as_dict(item) for item in stock)
        if _as_str(row.get("variantId")) == variant_id
    ]
    known = [amount for amount in amounts if amount is not None]
    if not known:
        return True
    return any(amount > 0 for amount in known)


def _image_url(photo: object) -> str | None:
    """Turn a photo record into an absolute CDN URL.

    Args:
        photo: The ``photo`` object of a gallery entry or a variant.

    Returns:
        The absolute URL, or None when the record names no path.
    """
    record = _as_dict(photo)
    path = _as_str(record.get("path"))
    host = FILE_BUCKETS.get(_as_str(record.get("bucket")) or "", FILE_BUCKETS["Upload"])
    return f"{host}/{path}" if path else None


def _label_lines(product: dict[str, object]) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Split the plain-text description into labelled values and prose.

    The shop writes its whole parameter sheet as ``Label: value`` lines inside
    the description, so this is where origin, altitude and process come from.

    Args:
        product: One product record.

    Returns:
        A ``(raw_attributes, folded_lookup, prose)`` tuple.
    """
    text = _localised(product.get("plainDescription")) or ""
    parsed = dom.LabelledLines()
    for line in (candidate.strip() for candidate in text.splitlines()):
        if not line:
            continue
        match = dom.LABEL_RE.match(line)
        if match is None or _URL_LABEL_RE.search(match.group("label")):
            parsed.prose.append(line)
            continue
        parsed.pairs.append((match.group("label").strip(), match.group("value").strip()))
    return parsed.raw(), parsed.folded(), parsed.prose


def _pick(labels: dict[str, str], names: tuple[str, ...]) -> str | None:
    """Return the first label a product states out of several spellings.

    Args:
        labels: The folded label lookup.
        names: The spellings to try, in priority order.

    Returns:
        The value, or None when the product states none of them.
    """
    return next((labels[name] for name in names if name in labels), None)


@register
class FathersSite(SiteAdapter):
    """fathers.cz — a Czech roastery whose SvelteKit storefront ships its data."""

    site_id = "fathers"
    name = "Father's Coffee Roastery"
    country = "CZ"
    base_url = BASE_URL
    kind = "roaster"
    #: The catalogue is not paginated; the cap only guards against a redesign.
    max_pages = 1

    def ignored_names(self) -> tuple[str, ...]:
        """Return the folded markers of products that are not whole beans.

        Returns:
            The default markers plus the shop's capsules, drip bags and boxes.
        """
        return IGNORED_MARKERS

    def listing_url(self) -> str:
        """Return the URL of the one page that lists every product.

        Returns:
            The absolute listing URL.
        """
        return urljoin(BASE_URL, LISTING_PATH)

    def parse_listing(self, html: str) -> list[ProductRef]:
        """Turn the catalogue page into references that already carry their data.

        The page embeds each product in full, so every reference gets a
        ``payload`` and the pipeline never requests a detail page.

        Args:
            html: The listing page source.

        Returns:
            One reference per coffee product, in page order.
        """
        props = parse_props(html)
        slugs = self._subcategory_slugs(props)
        currency = _as_str(props.get("currency")) or DEFAULT_CURRENCY
        destination = _as_str(props.get("destinationCountry")) or DEFAULT_DESTINATION
        refs: list[ProductRef] = []
        seen: set[str] = set()
        for item in _as_list(props.get("products")):
            product = _as_dict(item)
            slug = slugs.get(_as_str(product.get("subcategoryId")) or "")
            external_id = _as_str(product.get("id"))
            if slug is None or slug not in COFFEE_SUBCATEGORIES or external_id in seen:
                continue
            if external_id is None:
                logger.debug("skipping a product record without an id")
                continue
            seen.add(external_id)
            refs.append(self._reference(product, slug, currency, destination))
        return refs

    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        """Fetch the catalogue page and yield every coffee it lists.

        Args:
            fetcher: The shared polite fetcher.
            max_pages: Ignored beyond zero, which yields nothing; the catalogue
                is a single page.

        Yields:
            One reference per coffee product.
        """
        cap = max_pages if max_pages is not None else self.max_pages
        if cap < 1:
            return
        result = fetcher.get(self.listing_url())
        yield from self.parse_listing(result.text)

    def parse_product(self, html: str, ref: ProductRef) -> Coffee | None:
        """Turn one product record into a coffee.

        Accepts both the JSON a reference carries and a detail page's own source.

        Args:
            html: The product payload, or the detail page source.
            ref: What discovery already told us about this product.

        Returns:
            The parsed coffee, or None for capsules, drip bags and tasting
            boxes, which the coffee categories list alongside whole beans.
        """
        props = parse_props(html)
        product = _as_dict(props.get("product"))
        if not product:
            logger.debug("no product record in the source of %s", ref.url)
            return None
        slug = self._detail_slug(props, ref)
        name = _localised(product.get("name")) or ref.name or ""
        if self.is_ignored(name):
            logger.debug("%s is not whole beans, skipping", name)
            return None
        currency = _as_str(props.get("currency")) or ref.currency or DEFAULT_CURRENCY
        destination = _as_str(props.get("destinationCountry")) or DEFAULT_DESTINATION
        raw_attributes, labels, prose = _label_lines(product)
        variants = self._variants(product, ref, currency, destination)
        species = self._species(name, labels, prose)
        return self._build(
            product=product,
            ref=ref,
            name=name,
            slug=slug,
            currency=currency,
            variants=variants,
            species=species,
            labels=labels,
            prose=prose,
            raw_attributes=raw_attributes,
        )

    # ------------------------------------------------------------------ pieces

    def _subcategory_slugs(self, props: dict[str, object]) -> dict[str, str]:
        """Map every subcategory id onto its URL slug.

        Args:
            props: The page payload.

        Returns:
            Subcategory id -> slug.
        """
        slugs: dict[str, str] = {}
        for item in _as_list(props.get("subcategories")):
            record = _as_dict(item)
            identifier = _as_str(record.get("id"))
            slug = _as_str(record.get("urlSlug_cs")) or _localised(record.get("urlSlug"))
            if identifier and slug:
                slugs[identifier] = slug
        return slugs

    def _detail_slug(self, props: dict[str, object], ref: ProductRef) -> str | None:
        """Work out which coffee subcategory a detail page belongs to.

        Args:
            props: The page payload.
            ref: The reference being parsed.

        Returns:
            The subcategory slug, or None when neither source names one.
        """
        subcategory = _as_dict(props.get("subcategory"))
        from_page = _as_str(subcategory.get("urlSlug_cs")) or _localised(subcategory.get("urlSlug"))
        return from_page or ref.extra.get("subcategory")

    def product_url(self, slug: str | None, product: dict[str, object]) -> str:
        """Build a product's public URL out of its slugs.

        Args:
            slug: The subcategory slug.
            product: The product record.

        Returns:
            The absolute URL.
        """
        slugs = (product.get("urlSlug_cs"), product.get("urlSlug"))
        product_slug = next((text for text in map(_localised, slugs) if text), "")
        parts = [part for part in ("kava", slug, product_slug) if part]
        return urljoin(BASE_URL, "/".join(parts))

    def _reference(
        self,
        product: dict[str, object],
        slug: str,
        currency: str,
        destination: str,
    ) -> ProductRef:
        """Build one reference, payload included, from a listing record.

        Args:
            product: The product record.
            slug: Its subcategory slug.
            currency: The currency the page quotes.
            destination: The country whose VAT rate applies.

        Returns:
            The reference.
        """
        variants = _as_list(product.get("variants"))
        first = _as_dict(variants[0]) if variants else {}
        price, _ = _price_for(first.get("prices"), currency, destination)
        payload = {
            "product": product,
            "subcategory": {"urlSlug_cs": slug},
            "currency": currency,
            "destinationCountry": destination,
        }
        gallery = _as_list(product.get("gallery"))
        image = _image_url(_as_dict(gallery[0]).get("photo")) if gallery else None
        return ProductRef(
            site_id=self.site_id,
            external_id=_as_str(product.get("id")) or "",
            url=self.product_url(slug, product),
            name=_localised(product.get("name")),
            price=price,
            currency=currency if price is not None else None,
            image_url=image,
            extra={"subcategory": slug},
            payload=json.dumps(payload, ensure_ascii=False),
        )

    def _variants(
        self,
        product: dict[str, object],
        ref: ProductRef,
        currency: str,
        destination: str,
    ) -> list[Variant]:
        """Read every purchasable packaging option with its own price.

        Args:
            product: The product record.
            ref: The reference being parsed.
            currency: The currency the page quotes.
            destination: The country whose VAT rate applies.

        Returns:
            One variant per packaging option, in page order.
        """
        stock = _as_list(product.get("stock"))
        variants: list[Variant] = []
        for item in _as_list(product.get("variants")):
            record = _as_dict(item)
            identifier = _as_str(record.get("id"))
            label = _localised(record.get("label"))
            price, _ = _price_for(record.get("prices"), currency, destination)
            weight = _as_number(record.get("weightInGrams"))
            variants.append(
                Variant(
                    external_id=identifier,
                    url=f"{ref.url}?variant={identifier}" if identifier else ref.url,
                    weight_g=round(weight) if weight else normalize.parse_weight_grams(label),
                    price=price,
                    currency=currency if price is not None else None,
                    available=_stock_available(stock, identifier),
                    label=label,
                )
            )
        return variants

    def _species(self, name: str, labels: dict[str, str], prose: list[str]) -> Species:
        """Read the arabica/robusta split the description states in words.

        Args:
            name: The product name.
            labels: The folded label lookup.
            prose: Every description line that is not a labelled value.

        Returns:
            The composition.
        """
        blob = " ".join([_pick(labels, _LABEL_TYPE) or "", *prose])
        arabica, robusta = normalize.parse_species(blob)
        folded = normalize.fold(blob)
        if arabica is None and "arabi" in folded and "robus" not in folded:
            arabica, robusta = 100, 0
        return Species(
            arabica_pct=arabica,
            robusta_pct=robusta,
            is_blend=normalize.detect_blend(name, arabica, robusta),
        )

    def _origin(self, labels: dict[str, str], name: str) -> Origin:
        """Read the origin block out of the labelled description lines.

        Args:
            labels: The folded label lookup.
            name: The product name, used when no country label exists.

        Returns:
            The origin part of the model.
        """
        altitude_raw = _pick(labels, _LABEL_ALTITUDE)
        low, high = normalize.parse_altitude(altitude_raw)
        country_text = _pick(labels, _LABEL_COUNTRY)
        return Origin(
            country=normalize.detect_country(country_text) or normalize.detect_country(name),
            region=_pick(labels, _LABEL_REGION),
            farm=_pick(labels, _LABEL_FARM),
            producer=_pick(labels, _LABEL_PRODUCER),
            altitude_min_m=low,
            altitude_max_m=high,
            altitude_raw=altitude_raw,
            variety=normalize.clean_variety(normalize.split_list(_pick(labels, _LABEL_VARIETY))),
            harvest=_pick(labels, _LABEL_HARVEST),
        )

    def _taste(self, labels: dict[str, str], product: dict[str, object], slug: str | None) -> Taste:
        """Read the cup notes and the brewing style the roast targets.

        Args:
            labels: The folded label lookup.
            product: The product record.
            slug: The subcategory slug.

        Returns:
            The sensory part of the model.
        """
        notes = normalize.split_list(_pick(labels, _LABEL_NOTES))
        return Taste(
            flavor_notes=notes,
            tasting_text=_localised(product.get("metaDescription")),
            brewing_methods=[slug] if slug else [],
        )

    def _disclosures(self, product: dict[str, object]) -> dict[str, str]:
        """Keep the shop's long-form sections as ``raw_attributes``.

        Each section (farm story, processing, variety) is prose the typed model
        has no home for, but it is the richest text the shop publishes.

        Args:
            product: The product record.

        Returns:
            Upper-cased section title -> its plain text.
        """
        collected: dict[str, str] = {}
        sections = _as_dict(product.get("disclosures")).get(LANGUAGE)
        for item in _as_list(sections):
            record = _as_dict(item)
            title = _as_str(record.get("title"))
            content = _as_str(record.get("content"))
            if title and content:
                collected.setdefault(title.upper(), re.sub(r"<[^>]+>", " ", content).strip())
        return collected

    def _build(  # noqa: PLR0913  (one call site; splitting it would only hide the shape)
        self,
        *,
        product: dict[str, object],
        ref: ProductRef,
        name: str,
        slug: str | None,
        currency: str,
        variants: list[Variant],
        species: Species,
        labels: dict[str, str],
        prose: list[str],
        raw_attributes: dict[str, str],
    ) -> Coffee:
        """Assemble the coffee from the pieces the other helpers produced.

        Args:
            product: The product record.
            ref: The reference being parsed.
            name: The product name.
            slug: The subcategory slug.
            currency: The currency the page quotes.
            variants: The packaging options.
            species: The arabica/robusta split.
            labels: The folded label lookup.
            prose: Every description line that is not a labelled value.
            raw_attributes: The labels as written.

        Returns:
            The finished coffee.
        """
        first = variants[0] if variants else Variant()
        for key, value in self._disclosures(product).items():
            raw_attributes.setdefault(key, value)
        for key in ("metaKeywords", "metaDescription"):
            meta = _localised(product.get(key))
            if meta:
                raw_attributes.setdefault(key.upper(), meta)
        tags = ["Father's Choice"] if product.get("partOfFathersChoice") is True else []
        if _as_str(product.get("markedAsNewAt")):
            tags.append("Novinka")
        process_raw = _pick(labels, _LABEL_PROCESS)
        return Coffee(
            site=self.site_id,
            external_id=_as_str(product.get("id")) or ref.external_id,
            url=self.product_url(slug, product) or ref.url,
            name=name,
            site_country=self.country,
            price=first.price if first.price is not None else ref.price,
            currency=currency,
            weight_g=first.weight_g,
            available=any(variant.available for variant in variants) if variants else None,
            decaf="bezkofein" in normalize.fold(name) or "decaf" in normalize.fold(name),
            origin=self._origin(labels, name),
            processing=normalize.parse_processing(process_raw),
            roast=Roast(
                profile=_PROFILE_BY_SUBCATEGORY.get(slug or "", RoastProfile.UNKNOWN),
                raw=slug,
            ),
            species=species,
            taste=self._taste(labels, product, slug),
            variants=variants,
            images=dom.unique(
                _image_url(_as_dict(item).get("photo")) for item in _as_list(product.get("gallery"))
            ),
            tags=tags,
            categories=dom.unique(["Káva", slug]),
            specialty_grade=product.get("partOfFathersChoice") is True or None,
            description="\n".join(prose) or None,
            origin_text=_pick(labels, _LABEL_COUNTRY),
            raw_attributes=raw_attributes,
        )
