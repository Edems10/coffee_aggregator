from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from coffee_aggregator.money import per_kg

#: The scale every sensory bar is expressed on, and the default review scale.
DEFAULT_TASTE_SCALE_MAX = 5
DEFAULT_RATING_MAX = 5
#: The raw attribute both platform adapters already write the roastery into.
BRAND_ATTRIBUTE = "BRAND"
#: Two prices this close apart are the same price: shops state them to the cent,
#: and the halfpenny of a float round-trip must not break the match.
_PRICE_EPSILON = 0.005


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
