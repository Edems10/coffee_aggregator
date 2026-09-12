from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from coffee_aggregator import normalize

if TYPE_CHECKING:
    from collections.abc import Iterator

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee

DEFAULT_IGNORED = ("tasting pack", "cascara")


@dataclass(slots=True)
class ProductRef:
    """A product spotted on a listing page, before its detail page is fetched.

    Attributes:
        site_id: Registry id of the shop.
        external_id: The shop's own identifier for the product.
        url: Absolute URL of the detail page.
        name: Product name as shown on the listing, when available.
        price: Listing price, when available.
        currency: Currency of ``price``.
        image_url: Thumbnail spotted on the listing.
        extra: Any other listing-only strings worth carrying over.
        payload: The product's own source, when discovery already holds it —
            a JSON-API record or a feed entry. When set the pipeline parses
            it directly and never requests ``url``.
    """

    site_id: str
    external_id: str
    url: str
    name: str | None = None
    price: float | None = None
    currency: str | None = None
    image_url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)
    payload: str | None = None


class SiteAdapter(ABC):
    """One shop: how to enumerate its products and how to read one page.

    Subclasses must be constructible with no arguments so the registry can
    instantiate them, and :meth:`parse_product` must stay pure — the pipeline
    owns every HTTP request.
    """

    site_id: str = ""
    name: str = ""
    country: Literal["CZ", "SK"] = "SK"
    base_url: str = ""
    kind: str = "bespoke"
    max_pages: int = 100

    @abstractmethod
    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        """Walk the shop's listings and yield one reference per product.

        Args:
            fetcher: The shared polite fetcher.
            max_pages: Cap on listing pages for this run only. Adapters are
                registry singletons, so a per-run cap is passed in rather than
                written onto ``self.max_pages``; None means "use the adapter's
                own cap".

        Yields:
            One reference per product found.
        """

    @abstractmethod
    def parse_product(self, html: str, ref: ProductRef) -> Coffee | None:
        """Turn one detail page into a :class:`~coffee_aggregator.models.Coffee`.

        Partial data is fine — unmapped labels belong in ``raw_attributes``.

        Args:
            html: The detail page source.
            ref: What the listing page already told us about this product.

        Returns:
            The parsed coffee, or None when the product is not coffee beans
            (cascara, merchandise, tasting packs).
        """

    def is_ignored(self, name: str | None) -> bool:
        """Decide whether a product name marks a non-coffee item.

        Matched on folded text so Slovak diacritics never hide a marker; the
        markers themselves must therefore already be folded.

        Args:
            name: The product name, when known.

        Returns:
            True when the product should be skipped entirely.
        """
        folded = normalize.fold(name)
        return bool(folded) and any(marker in folded for marker in self.ignored_names())

    def ignored_names(self) -> tuple[str, ...]:
        """Return the lower-case markers that mark a product as non-coffee.

        Returns:
            The default ignore list; override to extend it per shop.
        """
        return DEFAULT_IGNORED
