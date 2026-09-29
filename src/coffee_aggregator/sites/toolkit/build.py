from __future__ import annotations

from typing import TYPE_CHECKING, Final

from coffee_aggregator import normalize
from coffee_aggregator.models import Variant
from coffee_aggregator.sites import html as dom

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

__all__ = ["gallery", "keep", "package", "schema_stock", "stock_state"]

#: What a shop writes when the bag is there, folded. Czech and Slovak both, so
#: no shop has to restate the two words its own language happens to use.
_IN_STOCK: Final = ("skladom", "skladem", "in stock")
#: What a shop writes when it is not. Tested first, which is what makes the
#: negations work: "není skladem" holds "skladem" and means the opposite.
_OUT_OF_STOCK: Final = (
    "neni skladem",
    "nie je skladom",
    "vypredane",
    "vyprodano",
    "nedostupne",
    "sold out",
)


def stock_state(*texts: str | None) -> bool | None:
    """Decide whether a shop's stock wording means the bag is there.

    Args:
        texts: Every stock wording the page states — the label, the delivery
            note, the schema.org value — in any order.

    Returns:
        True, False, or None when nothing said either way.
    """
    blob = normalize.fold(" ".join(text for text in texts if text))
    if not blob:
        return None
    if any(marker in blob for marker in _OUT_OF_STOCK):
        return False
    if any(marker in blob for marker in _IN_STOCK):
        return True
    return None


def schema_stock(value: str | None) -> bool | None:
    """Read a schema.org ``availability`` value.

    Args:
        value: The ``content`` of the availability meta, when the page has one.

    Returns:
        True for ``InStock``, False for any other stated value, None when the
        page states none — the vocabulary is closed, so anything else is one of
        the sold-out spellings.
    """
    folded = normalize.fold(value).replace(" ", "")
    if not folded:
        return None
    return "instock" in folded


def package(  # noqa: PLR0913  (one variant field per argument; grouping them hides the shape)
    *,
    external_id: str | None,
    url: str,
    label: str | None,
    price: float | None,
    currency: str,
    weight_g: int | None = None,
    available: bool | None = None,
) -> Variant:
    """Build one packaging option, with the weight read off its own label.

    Every shop renders its size axis as a label ("250 g", "1 kg") beside a
    price, so the weight is read from the label unless the shop states a number
    of its own, and the currency is kept only when a price came with it.

    Args:
        external_id: The shop's id for this option.
        url: The URL that selects this option.
        label: The option label as the shop wrote it.
        price: The option's own price.
        currency: The shop's currency, kept only when ``price`` is set.
        weight_g: A weight the shop states outright, which wins over the label.
        available: Whether this option is in stock, when the shop says.

    Returns:
        The variant.
    """
    return Variant(
        external_id=external_id,
        url=url,
        weight_g=weight_g if weight_g is not None else normalize.parse_weight_grams(label),
        price=price,
        currency=currency if price is not None else None,
        available=available,
        label=label,
    )


def keep(raw: dict[str, str], extra: Mapping[str, str]) -> None:
    """Add page-level strings to ``raw_attributes`` without overwriting a label.

    Open Graph and ``<meta>`` routinely name the origin or the cup notes that
    the visible markup states nowhere else, but a real label is always the
    better source, so these only ever fill a gap.

    Args:
        raw: The raw attributes collected so far, modified in place.
        extra: The strings to keep, already upper-cased.
    """
    for key, value in extra.items():
        if value:
            raw.setdefault(key, value)


def gallery(
    base_url: str,
    candidates: Iterable[str | None],
    *,
    keep_when: Callable[[str], bool] | None = None,
) -> list[str]:
    """Turn a page's image candidates into the absolute URLs worth storing.

    Every shop states the same photo three times — gallery link, thumbnail,
    Open Graph — and pins its award badges into the same gallery, so the list
    is de-duplicated in page order and filtered on the raw URL, which is where
    a shop's badge directory shows.

    Args:
        base_url: The shop's base URL.
        candidates: Hrefs and srcs as written, in the order they should win.
        keep_when: Decides on the raw URL whether a candidate is a product
            photo; everything is kept when it is None.

    Returns:
        The absolute URLs, de-duplicated, in page order.
    """
    return dom.unique(
        dom.absolute(base_url, url)
        for url in candidates
        if url and (keep_when is None or keep_when(url))
    )
