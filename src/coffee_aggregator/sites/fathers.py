from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin

from coffee_aggregator import normalize
from coffee_aggregator.labels import (
    F_COUNTRY,
    F_FARM,
    F_FLAVOR,
    F_PROCESS,
    F_SPECIES,
    label_pair,
    parse_origin,
)
from coffee_aggregator.models import Coffee, Roast, RoastProfile, Species, Taste, Variant
from coffee_aggregator.sites import html as dom
from coffee_aggregator.sites import toolkit as kit
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
#: Any HTML a long-form section wraps its prose in.
_TAG_RE: Final = re.compile(r"<[^>]+>")

#: The shop's one private spelling: it heads the farm row "Stanice" on the lots
#: whose washing station is the only place named, where the shared vocabulary
#: knows only the full "zpracovatelská stanice".
SHOP_TERMS: Final[dict[str, str]] = {"stanice": F_FARM}
LABEL_MAP: Final[dict[str, str]] = kit.vocabulary(SHOP_TERMS)

#: Folded subcategory slug -> the roast profile it stands for.
_PROFILE_BY_SUBCATEGORY: Final = {
    "espresso": RoastProfile.ESPRESSO,
    "filtr": RoastProfile.FILTER,
    "dripbagy": RoastProfile.FILTER,
}


def _localised(value: object) -> str | None:
    """Read the Czech text out of one of the shop's localised fields.

    Args:
        value: The localised field, which may also be a plain string.

    Returns:
        The Czech text, falling back to any other language.
    """
    return kit.localised(value, LANGUAGE)


def parse_props(text: str) -> dict[str, object]:
    """Read the ``#props`` payload the storefront embeds in every page.

    Args:
        text: A page source, or the JSON document a :class:`ProductRef` carries.

    Returns:
        The decoded payload; empty when the text holds neither.
    """
    if kit.looks_like_json(text):
        return kit.json_object(text, what="product payload")
    return kit.embedded_json(text, _PROPS_RE, what="#props script")


def _price_for(
    prices: object,
    currency: str,
    destination: str,
) -> float | None:
    """Pick the price a buyer in one country pays, out of the VAT-rate matrix.

    Args:
        prices: The variant's ``prices`` mapping, keyed by currency then rate.
        currency: The currency to read.
        destination: The country whose VAT rate applies.

    Returns:
        The price including tax, or None when nothing matches.
    """
    by_rate = kit.as_dict(kit.as_dict(prices).get(currency))
    fallback: float | None = None
    for entry in by_rate.values():
        record = kit.as_dict(entry)
        with_tax = kit.as_number(kit.as_dict(record.get("price")).get("withTax"))
        countries = [kit.as_str(item) for item in kit.as_list(record.get("countries"))]
        if destination in countries:
            return with_tax
        if fallback is None:
            fallback = with_tax
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
        kit.as_number(row.get("amount"))
        for row in (kit.as_dict(item) for item in stock)
        if kit.as_str(row.get("variantId")) == variant_id
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
    record = kit.as_dict(photo)
    path = kit.as_str(record.get("path"))
    host = FILE_BUCKETS.get(kit.as_str(record.get("bucket")) or "", FILE_BUCKETS["Upload"])
    return f"{host}/{path}" if path else None


def _named_pairs(line: str, _label_map: dict[str, str]) -> list[tuple[str, str]]:
    """Read one description line, refusing a bracketed link as a label.

    The shop writes its markdown links as ``[text](https://…)``, whose colon
    reads as a label separator and whose "label" is then the sentence in front
    of the link.

    Args:
        line: One line of the plain-text description.
        _label_map: Unused; the signature is the one the line reader takes.

    Returns:
        A one-element list, or an empty one when the line is prose.
    """
    pairs = label_pair(line)
    return [] if pairs and _URL_LABEL_RE.search(pairs[0][0]) else pairs


def _label_lines(product: dict[str, object]) -> tuple[kit.Facts, list[str]]:
    """Split the plain-text description into labelled values and prose.

    The shop writes its whole parameter sheet as ``Label: value`` lines inside
    the description, so this is where origin, altitude and process come from.

    Args:
        product: One product record.

    Returns:
        The labelled facts, and every line that is not one.
    """
    text = _localised(product.get("plainDescription")) or ""
    lines = [line for line in (candidate.strip() for candidate in text.splitlines()) if line]
    # The sheet and the shop's story share one text, so a heading is a heading.
    return kit.read_text(lines, LABEL_MAP, _named_pairs, bare_labels=False)


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
        currency = kit.as_str(props.get("currency")) or DEFAULT_CURRENCY
        destination = kit.as_str(props.get("destinationCountry")) or DEFAULT_DESTINATION
        refs: list[ProductRef] = []
        seen: set[str] = set()
        for item in kit.as_list(props.get("products")):
            product = kit.as_dict(item)
            slug = slugs.get(kit.as_str(product.get("subcategoryId")) or "")
            external_id = kit.as_str(product.get("id"))
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
        yield from kit.walk_listing(fetcher, [self.listing_url()], self.parse_listing)

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
        product = kit.as_dict(props.get("product"))
        if not product:
            logger.debug("no product record in the source of %s", ref.url)
            return None
        slug = self._detail_slug(props, ref)
        name = _localised(product.get("name")) or ref.name or ""
        if self.is_ignored(name):
            logger.debug("%s is not whole beans, skipping", name)
            return None
        currency = kit.as_str(props.get("currency")) or ref.currency or DEFAULT_CURRENCY
        destination = kit.as_str(props.get("destinationCountry")) or DEFAULT_DESTINATION
        facts, prose = _label_lines(product)
        variants = self._variants(product, ref, currency, destination)
        species = self._species(name, facts, prose)
        return self._build(
            product=product,
            ref=ref,
            name=name,
            slug=slug,
            currency=currency,
            variants=variants,
            species=species,
            facts=facts,
            prose=prose,
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
        for item in kit.as_list(props.get("subcategories")):
            record = kit.as_dict(item)
            identifier = kit.as_str(record.get("id"))
            slug = kit.as_str(record.get("urlSlug_cs")) or _localised(record.get("urlSlug"))
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
        subcategory = kit.as_dict(props.get("subcategory"))
        from_page = kit.as_str(subcategory.get("urlSlug_cs")) or _localised(
            subcategory.get("urlSlug")
        )
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
        variants = kit.as_list(product.get("variants"))
        first = kit.as_dict(variants[0]) if variants else {}
        payload = {
            "product": product,
            "subcategory": {"urlSlug_cs": slug},
            "currency": currency,
            "destinationCountry": destination,
        }
        gallery = kit.as_list(product.get("gallery"))
        return kit.product_ref(
            self.site_id,
            kit.as_str(product.get("id")) or "",
            self.product_url(slug, product),
            name=_localised(product.get("name")),
            price=_price_for(first.get("prices"), currency, destination),
            currency=currency,
            image_url=_image_url(kit.as_dict(gallery[0]).get("photo")) if gallery else None,
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
        stock = kit.as_list(product.get("stock"))
        variants: list[Variant] = []
        for item in kit.as_list(product.get("variants")):
            record = kit.as_dict(item)
            identifier = kit.as_str(record.get("id"))
            weight = kit.as_number(record.get("weightInGrams"))
            variants.append(
                kit.package(
                    external_id=identifier,
                    url=f"{ref.url}?variant={identifier}" if identifier else ref.url,
                    label=_localised(record.get("label")),
                    price=_price_for(record.get("prices"), currency, destination),
                    currency=currency,
                    weight_g=round(weight) if weight else None,
                    available=_stock_available(stock, identifier),
                )
            )
        return variants

    def _species(self, name: str, facts: kit.Facts, prose: list[str]) -> Species:
        """Read the arabica/robusta split the description states in words.

        Args:
            name: The product name.
            facts: The labelled values of the description.
            prose: Every description line that is not a labelled value.

        Returns:
            The composition.
        """
        blob = " ".join([facts.get(F_SPECIES) or "", *prose])
        arabica, robusta = normalize.parse_species(blob)
        folded = normalize.fold(blob)
        if arabica is None and "arabi" in folded and "robus" not in folded:
            arabica, robusta = 100, 0
        return Species(
            arabica_pct=arabica,
            robusta_pct=robusta,
            is_blend=normalize.detect_blend(name, arabica, robusta),
        )

    def _taste(self, facts: kit.Facts, product: dict[str, object], slug: str | None) -> Taste:
        """Read the cup notes and the brewing style the roast targets.

        Args:
            facts: The labelled values of the description.
            product: The product record.
            slug: The subcategory slug.

        Returns:
            The sensory part of the model.
        """
        return Taste(
            flavor_notes=normalize.split_list(facts.get(F_FLAVOR)),
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
        sections = kit.as_dict(product.get("disclosures")).get(LANGUAGE)
        for item in kit.as_list(sections):
            record = kit.as_dict(item)
            title = kit.as_str(record.get("title"))
            content = kit.as_str(record.get("content"))
            if title and content:
                collected.setdefault(title.upper(), _TAG_RE.sub(" ", content).strip())
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
        facts: kit.Facts,
        prose: list[str],
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
            facts: The labelled values of the description.
            prose: Every description line that is not a labelled value.

        Returns:
            The finished coffee.
        """
        first = variants[0] if variants else Variant()
        kit.keep(facts.raw, self._disclosures(product))
        kit.keep(
            facts.raw,
            {
                key.upper(): _localised(product.get(key)) or ""
                for key in ("metaKeywords", "metaDescription")
            },
        )
        tags = ["Father's Choice"] if product.get("partOfFathersChoice") is True else []
        if kit.as_str(product.get("markedAsNewAt")):
            tags.append("Novinka")
        return Coffee(
            site=self.site_id,
            external_id=kit.as_str(product.get("id")) or ref.external_id,
            url=self.product_url(slug, product) or ref.url,
            name=name,
            site_country=self.country,
            price=first.price if first.price is not None else ref.price,
            currency=currency,
            weight_g=first.weight_g,
            available=any(variant.available for variant in variants) if variants else None,
            decaf="bezkofein" in normalize.fold(name) or "decaf" in normalize.fold(name),
            origin=parse_origin(facts.labels, name, blend=species.is_blend),
            processing=normalize.parse_processing(facts.get(F_PROCESS)),
            roast=Roast(
                profile=_PROFILE_BY_SUBCATEGORY.get(slug or "", RoastProfile.UNKNOWN),
                raw=slug,
            ),
            species=species,
            taste=self._taste(facts, product, slug),
            variants=variants,
            images=dom.unique(
                _image_url(kit.as_dict(item).get("photo"))
                for item in kit.as_list(product.get("gallery"))
            ),
            tags=tags,
            categories=dom.unique(["Káva", slug]),
            specialty_grade=product.get("partOfFathersChoice") is True or None,
            description="\n".join(prose) or None,
            origin_text=facts.get(F_COUNTRY),
            raw_attributes=facts.raw,
        )
