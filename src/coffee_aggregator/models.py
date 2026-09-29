from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

GRAMS_PER_KG = 1000.0
#: The scale every sensory bar is expressed on, and the default review scale.
DEFAULT_TASTE_SCALE_MAX = 5
DEFAULT_RATING_MAX = 5
#: The raw attribute both platform adapters already write the roastery into.
BRAND_ATTRIBUTE = "BRAND"
#: Two prices this close apart are the same price: shops state them to the cent,
#: and the halfpenny of a float round-trip must not break the match.
_PRICE_EPSILON = 0.005
_CENTS = Decimal("0.01")


def per_kg(amount: float | None, weight_g: int | None) -> float | None:
    """Extrapolate an amount for one package to one kilogram.

    Args:
        amount: The price of one package, in any currency.
        weight_g: The net weight of that same package.

    Returns:
        The price of a kilogram rounded to the cent, or None when either input
        is missing or the weight is zero.
    """
    if amount is None or not weight_g:
        return None
    try:
        value = Decimal(str(amount)) * Decimal(str(GRAMS_PER_KG)) / Decimal(weight_g)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return float(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


@dataclass(slots=True, frozen=True)
class PriceBasis:
    """A price and the weight that price is for, taken from one single offer.

    Every per-kilogram number in the project is computed from one of these and
    from nothing else, which is what makes it impossible to divide the price of
    a 250 g bag by the weight the product name happened to mention.

    Attributes:
        price: The amount asked for one package.
        weight_g: The net weight of that package.
        currency: The ISO code that amount is in.
        source: ``"variant"`` when a packaging option stated both numbers,
            ``"product"`` when only the product-level pair was available.
    """

    price: float
    weight_g: int
    currency: str | None
    source: str

    def amount_per_kg(self) -> float | None:
        """Return the price of a kilogram in :attr:`currency`.

        Returns:
            The extrapolated amount, rounded to the cent.
        """
        return per_kg(self.price, self.weight_g)


class ProcessMethod(StrEnum):
    """How the cherry was turned into green coffee."""

    WASHED = "washed"
    NATURAL = "natural"
    HONEY = "honey"
    ANAEROBIC = "anaerobic"
    WET_HULLED = "wet_hulled"
    PULPED_NATURAL = "pulped_natural"
    EXPERIMENTAL = "experimental"
    #: Several distinct methods in one lot or one blend, e.g. "washed · natural".
    MIXED = "mixed"
    OTHER = "other"
    UNKNOWN = "unknown"


class RoastLevel(StrEnum):
    """How dark the beans were roasted."""

    LIGHT = "light"
    MEDIUM_LIGHT = "medium_light"
    MEDIUM = "medium"
    MEDIUM_DARK = "medium_dark"
    DARK = "dark"
    UNKNOWN = "unknown"


class RoastProfile(StrEnum):
    """Which brewing style the roast was built for."""

    ESPRESSO = "espresso"
    FILTER = "filter"
    OMNI = "omni"
    UNKNOWN = "unknown"


def _date_value(value: date | None, *, json_safe: bool) -> date | str | None:
    if value is None:
        return None
    return value.isoformat() if json_safe else value


@dataclass(slots=True)
class Variant:
    """One purchasable packaging option of a coffee."""

    external_id: str | None = None
    url: str | None = None
    weight_g: int | None = None
    price: float | None = None
    currency: str | None = None
    available: bool | None = None
    label: str | None = None
    #: Filled by the pipeline's derive step from the day's fixing, never by an
    #: adapter: a shop states one price, in one currency.
    price_eur: float | None = None
    price_czk: float | None = None
    #: Filled by the same step, from this variant's own price and weight — never
    #: from the product's, which may describe a different package entirely.
    price_per_kg_eur: float | None = None
    price_per_kg_czk: float | None = None

    def key(self, index: int = 0) -> str:
        """Return the identifier this variant is stored under.

        The key has to survive the next crawl, so the shop's own id comes first
        and the position in the list is only ever the last resort.

        Args:
            index: Where this variant sits in its product's list.

        Returns:
            A non-empty key, unique within one product as long as the shop does
            not state two identical options.
        """
        for candidate in (self.external_id, self.label):
            if candidate and candidate.strip():
                return candidate.strip()
        if self.weight_g:
            return f"{self.weight_g}g"
        return f"#{index}"

    def to_record(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of this variant.

        Returns:
            A flat dictionary safe to store in a jsonb column.
        """
        return {
            "external_id": self.external_id,
            "url": self.url,
            "weight_g": self.weight_g,
            "price": self.price,
            "currency": self.currency,
            "available": self.available,
            "label": self.label,
            "price_eur": self.price_eur,
            "price_czk": self.price_czk,
            "price_per_kg_eur": self.price_per_kg_eur,
            "price_per_kg_czk": self.price_per_kg_czk,
        }


@dataclass(slots=True)
class Origin:
    """Where the green coffee came from."""

    country: str | None = None
    region: str | None = None
    farm: str | None = None
    producer: str | None = None
    washing_station: str | None = None
    altitude_min_m: int | None = None
    altitude_max_m: int | None = None
    altitude_raw: str | None = None
    variety: list[str] = field(default_factory=list)
    harvest: str | None = None


@dataclass(slots=True)
class Processing:
    """Post-harvest processing of the green coffee.

    Attributes:
        method: The single method, ``MIXED`` when the lot names several, and
            ``UNKNOWN``/``OTHER`` when none could be recognised.
        raw: The text the shop wrote, verbatim.
        methods: Every distinct recognised method, in the order they appear —
            a blend of a washed and a natural component keeps both.
    """

    method: ProcessMethod = ProcessMethod.UNKNOWN
    raw: str | None = None
    methods: list[ProcessMethod] = field(default_factory=list)


@dataclass(slots=True)
class Roast:
    """Roast level, intended brewing profile and freshness dates."""

    level: RoastLevel = RoastLevel.UNKNOWN
    raw: str | None = None
    profile: RoastProfile = RoastProfile.UNKNOWN
    roast_date: date | None = None
    best_before: date | None = None


@dataclass(slots=True)
class Species:
    """Arabica / robusta composition."""

    arabica_pct: int | None = None
    robusta_pct: int | None = None
    other: str | None = None
    is_blend: bool = False


@dataclass(slots=True)
class Review:
    """A single customer review."""

    author: str | None = None
    date: date | None = None
    rating: float | None = None
    text: str | None = None

    def to_record(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of this review.

        Returns:
            A flat dictionary safe to store in a jsonb column.
        """
        return {
            "author": self.author,
            "date": _date_value(self.date, json_safe=True),
            "rating": self.rating,
            "text": self.text,
        }


@dataclass(slots=True)
class Taste:
    """Sensory description of the cup."""

    body: int | None = None
    bitterness: int | None = None
    acidity: int | None = None
    sweetness: int | None = None
    scale_max: int = DEFAULT_TASTE_SCALE_MAX
    flavor_notes: list[str] = field(default_factory=list)
    tasting_text: str | None = None
    brewing_methods: list[str] = field(default_factory=list)
    sca_score: float | None = None


@dataclass(slots=True)
class Popularity:
    """Shop-side social proof."""

    rating: float | None = None
    rating_max: int = DEFAULT_RATING_MAX
    review_count: int | None = None
    reviews: list[Review] = field(default_factory=list)
    sold_count: int | None = None


@dataclass(slots=True)
class Coffee:
    """One coffee product as offered by one shop."""

    site: str
    external_id: str
    url: str
    name: str
    site_country: str
    price: float | None = None
    currency: str | None = None
    weight_g: int | None = None
    available: bool | None = None
    decaf: bool = False
    origin: Origin = field(default_factory=Origin)
    processing: Processing = field(default_factory=Processing)
    roast: Roast = field(default_factory=Roast)
    species: Species = field(default_factory=Species)
    taste: Taste = field(default_factory=Taste)
    popularity: Popularity = field(default_factory=Popularity)
    variants: list[Variant] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    awards: list[str] = field(default_factory=list)
    specialty_grade: bool | None = None
    original_price: float | None = None
    description: str | None = None
    origin_text: str | None = None
    raw_attributes: dict[str, str] = field(default_factory=dict)
    scraped_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: The roastery behind the coffee, which is what makes the same lot in two
    #: shops the same lot. An adapter that knows it may set it; every adapter
    #: that only writes the ``BRAND`` raw attribute is read through
    #: :attr:`roaster_name` instead, so none of them has to change.
    roaster: str | None = None
    #: Price normalisation. All six are filled by the pipeline's derive step from
    #: the day's EUR/CZK fixing, so a Czech and a Slovak shop can be compared in
    #: one query; an adapter never touches them.
    price_eur: float | None = None
    price_czk: float | None = None
    price_per_kg_eur: float | None = None
    price_per_kg_czk: float | None = None
    fx_rate_eur_czk: float | None = None
    fx_date: date | None = None

    @property
    def roaster_name(self) -> str | None:
        """The roastery as the shop wrote it.

        Returns:
            The explicit :attr:`roaster`, else the ``BRAND`` raw attribute both
            platform adapters already collect, else None.
        """
        written = self.roaster or self.raw_attributes.get(BRAND_ATTRIBUTE) or ""
        return written.strip() or None

    @property
    def roaster_key(self) -> str | None:
        """The roastery folded down to what two shops can be matched on.

        Returns:
            The de-accented, lower-cased name, or None when no roastery is known.
        """
        from coffee_aggregator import normalize  # noqa: PLC0415  (normalize imports this module)

        return normalize.fold(self.roaster_name) or None

    @property
    def price_basis(self) -> PriceBasis | None:
        """The one offer every per-kilogram number of this product is taken from.

        A shop states a price for the package it currently shows and a weight
        wherever it likes — in the name, in a parameter table, in a variant
        select. Dividing the one by the other is only sound when both describe
        the same package, so they are picked in this order:

        1. the packaging options that state this product's own price together
           with a weight of their own: when they agree on one weight, that pair
           is certainly one option;
        2. otherwise the product's own price and weight, when no option
           contradicted them;
        3. nothing, when two options state the same price for different weights —
           the product page simply does not say which bag the price is for, and
           a made-up answer is worse than an empty column.

        Returns:
            The basis, or None when this product has no sound one.
        """
        if self.price is None:
            return None
        weights = {
            variant.weight_g
            for variant in self.variants
            if variant.weight_g
            and variant.price is not None
            and abs(variant.price - self.price) <= _PRICE_EPSILON
        }
        if len(weights) == 1:
            return self._variant_basis(weights.pop())
        if weights:  # the same price for two different bags: unusable
            return None
        if not self.weight_g:
            return None
        return PriceBasis(self.price, self.weight_g, self.currency, "product")

    def _variant_basis(self, weight_g: int) -> PriceBasis:
        """Build the basis for a weight one of the variants agreed on.

        Args:
            weight_g: The weight those variants state.

        Returns:
            The basis, carrying the variant's currency when it states one.
        """
        assert self.price is not None  # noqa: S101  (only reached from price_basis)
        currency = next(
            (
                variant.currency
                for variant in self.variants
                if variant.weight_g == weight_g and variant.currency
            ),
            None,
        )
        return PriceBasis(self.price, weight_g, currency or self.currency, "variant")

    @property
    def price_per_kg(self) -> float | None:
        """Price of one kilogram of this coffee, in the shop's own currency.

        This column mixes currencies and cannot be ordered by across shops;
        ``price_per_kg_eur`` and ``price_per_kg_czk`` are the comparable ones.

        Returns:
            The extrapolated price, or None when there is no sound
            :attr:`price_basis`.
        """
        basis = self.price_basis
        return None if basis is None else basis.amount_per_kg()

    def to_record(self, *, json_safe: bool = True) -> dict[str, Any]:
        """Flatten the coffee into one row matching the ``coffee`` table.

        The keys are exactly the schema's data columns, so a sink can feed the
        dictionary straight into an INSERT without reshaping it.

        Args:
            json_safe: When True (the JSONL sink) dates become ISO strings and
                enums become plain strings; when False (the PostgreSQL sink) the
                native ``date`` objects are kept so psycopg can adapt them.

        Returns:
            A flat dictionary keyed by column name.
        """
        return {
            "site": self.site,
            "external_id": self.external_id,
            "url": self.url,
            "name": self.name,
            "site_country": self.site_country,
            "roaster": self.roaster_name,
            "roaster_key": self.roaster_key,
            "price": self.price,
            "currency": self.currency,
            "weight_g": self.weight_g,
            "price_per_kg": self.price_per_kg,
            "price_eur": self.price_eur,
            "price_czk": self.price_czk,
            "price_per_kg_eur": self.price_per_kg_eur,
            "price_per_kg_czk": self.price_per_kg_czk,
            "fx_rate_eur_czk": self.fx_rate_eur_czk,
            "fx_date": _date_value(self.fx_date, json_safe=json_safe),
            "available": self.available,
            "decaf": self.decaf,
            "origin_country": self.origin.country,
            "origin_region": self.origin.region,
            "origin_farm": self.origin.farm,
            "origin_producer": self.origin.producer,
            "origin_washing_station": self.origin.washing_station,
            "altitude_min_m": self.origin.altitude_min_m,
            "altitude_max_m": self.origin.altitude_max_m,
            "altitude_raw": self.origin.altitude_raw,
            "variety": list(self.origin.variety),
            "harvest": self.origin.harvest,
            "process_method": str(self.processing.method),
            "process_methods": [str(method) for method in self.processing.methods],
            "process_raw": self.processing.raw,
            "roast_level": str(self.roast.level),
            "roast_raw": self.roast.raw,
            "roast_profile": str(self.roast.profile),
            "roast_date": _date_value(self.roast.roast_date, json_safe=json_safe),
            "best_before": _date_value(self.roast.best_before, json_safe=json_safe),
            "arabica_pct": self.species.arabica_pct,
            "robusta_pct": self.species.robusta_pct,
            "is_blend": self.species.is_blend,
            "body": self.taste.body,
            "bitterness": self.taste.bitterness,
            "acidity": self.taste.acidity,
            "sweetness": self.taste.sweetness,
            "taste_scale_max": self.taste.scale_max,
            "flavor_notes": list(self.taste.flavor_notes),
            "tasting_text": self.taste.tasting_text,
            "brewing_methods": list(self.taste.brewing_methods),
            "sca_score": self.taste.sca_score,
            "rating": self.popularity.rating,
            "rating_max": self.popularity.rating_max,
            "review_count": self.popularity.review_count,
            "reviews": [review.to_record() for review in self.popularity.reviews],
            "sold_count": self.popularity.sold_count,
            "variants": [variant.to_record() for variant in self.variants],
            "images": list(self.images),
            "tags": list(self.tags),
            "categories": list(self.categories),
            "certifications": list(self.certifications),
            "awards": list(self.awards),
            "specialty_grade": self.specialty_grade,
            "original_price": self.original_price,
            "description": self.description,
            "origin_text": self.origin_text,
            "raw_attributes": dict(self.raw_attributes),
            "scraped_at": self.scraped_at.isoformat() if json_safe else self.scraped_at,
        }
