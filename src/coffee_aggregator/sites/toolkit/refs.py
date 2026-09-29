from __future__ import annotations

from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from coffee_aggregator.sites.base import ProductRef

if TYPE_CHECKING:
    from re import Pattern

__all__ = ["id_from", "product_ref", "sitemap_refs"]


def id_from(url: str | None, pattern: Pattern[str]) -> str | None:
    """Read a shop's own product id out of a product URL.

    Every bespoke shop writes its id into the URL and nowhere near the markup,
    so the id is read back from the href the listing states rather than rebuilt
    from a name — a slug rebuilt from a name loses every apostrophe.

    Args:
        url: A product URL or href.
        pattern: A pattern whose first group, or whose ``id`` group, is the id.

    Returns:
        The id as a string, or None when the URL is not a product URL.
    """
    if not url:
        return None
    match = pattern.search(url)
    if match is None:
        return None
    return match.groupdict().get("id") or match.group(1)


def sitemap_refs(xml: str, site_id: str, pattern: Pattern[str]) -> list[ProductRef]:
    """Turn a sitemap into one reference per product location.

    Args:
        xml: The sitemap body.
        site_id: The registry id to stamp on every reference.
        pattern: The product-id pattern, as :func:`id_from` takes it.

    Returns:
        One reference per distinct product URL, in sitemap order.
    """
    soup = BeautifulSoup(xml, "xml")
    refs: list[ProductRef] = []
    seen: set[str] = set()
    for location in soup.find_all("loc"):
        url = location.get_text(strip=True)
        external_id = id_from(url, pattern)
        if external_id is None or external_id in seen:
            continue
        seen.add(external_id)
        refs.append(ProductRef(site_id=site_id, external_id=external_id, url=url))
    return refs


def product_ref(  # noqa: PLR0913  (one reference field per argument, as the dataclass has them)
    site_id: str,
    external_id: str,
    url: str,
    *,
    name: str | None = None,
    price: float | None = None,
    currency: str | None = None,
    image_url: str | None = None,
    extra: dict[str, str] | None = None,
    payload: str | None = None,
) -> ProductRef:
    """Build one listing reference, with the currency tied to the price.

    A currency without a price is a claim about nothing, and every shop that
    stated one anyway had to be corrected at the sink; the rule lives here so no
    new shop restates it.

    Args:
        site_id: The registry id of the shop.
        external_id: The shop's own product id.
        url: The absolute detail-page URL.
        name: The name the listing card shows.
        price: The listing price, when the card states one.
        currency: The shop's currency, kept only when ``price`` is set.
        image_url: The thumbnail the card shows.
        extra: Listing-only strings worth carrying to the detail parse.
        payload: The product's own source, when the listing already ships it, so
            the pipeline never requests the detail page.

    Returns:
        The reference.
    """
    return ProductRef(
        site_id=site_id,
        external_id=external_id,
        url=url,
        name=name,
        price=price,
        currency=currency if price is not None else None,
        image_url=image_url,
        extra=extra or {},
        payload=payload,
    )
