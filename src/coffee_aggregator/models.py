from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

GRAMS_PER_KG = 1000.0
#: The scale every sensory bar is expressed on, and the default review scale.
DEFAULT_TASTE_SCALE_MAX = 5
DEFAULT_RATING_MAX = 5


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
    def price_per_kg(self) -> float | None:
        """Price of one kilogram of this coffee.

        Returns:
            The extrapolated price, or None when price or weight is unknown.
        """
        if self.price is None or not self.weight_g:
            return None
        return round(self.price * GRAMS_PER_KG / self.weight_g, 2)

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
