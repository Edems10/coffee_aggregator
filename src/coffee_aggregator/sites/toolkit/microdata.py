from __future__ import annotations

from typing import TYPE_CHECKING

from coffee_aggregator import normalize
from coffee_aggregator.models import DEFAULT_RATING_MAX, Review
from coffee_aggregator.sites import html as dom

if TYPE_CHECKING:
    from bs4 import BeautifulSoup, Tag

__all__ = ["ratings", "reviews", "value"]

#: Where a review's own text lives, most specific first. Schema.org names it
#: ``reviewBody``; half the templates in the wild reuse ``description``.
_BODY_PROPERTIES = ("reviewBody", "description")


def value(scope: Tag | BeautifulSoup | None, name: str) -> str | None:
    """Read one schema.org property out of a subtree.

    A property is stated twice over: machine-readable in ``content`` and human-
    readable as text. The attribute wins because a template that prints "4,5"
    still writes ``content="4.5"``.

    Args:
        scope: The element the property belongs to.
        name: The ``itemprop`` name.

    Returns:
        The value, or None when the subtree states it nowhere.
    """
    if scope is None:
        return None
    tag = scope.select_one(f"[itemprop={name}]")
    return dom.attr(tag, "content") or dom.text(tag)


def reviews(scope: Tag | BeautifulSoup | None) -> list[Review]:
    """Read every customer review a subtree marks up.

    Args:
        scope: The block holding the reviews, e.g. the review list.

    Returns:
        One :class:`~coffee_aggregator.models.Review` per marked-up review, in
        page order; empty when the page publishes none.
    """
    if scope is None:
        return []
    collected: list[Review] = []
    for item in scope.select("[itemprop=review]"):
        body = next((found for name in _BODY_PROPERTIES if (found := value(item, name))), None)
        stars = value(item.select_one("[itemprop=reviewRating]"), "ratingValue")
        collected.append(
            Review(
                author=value(item.select_one("[itemprop=author]"), "name") or value(item, "author"),
                date=normalize.parse_date_dmy(value(item, "datePublished")),
                rating=normalize.parse_float(stars),
                text=body,
            )
        )
    return collected


def ratings(scope: Tag | BeautifulSoup | None) -> tuple[float | None, int | None, int]:
    """Read the aggregate rating a subtree states.

    Args:
        scope: The block holding the aggregate rating.

    Returns:
        The score, how many reviews it averages, and the scale it is on — the
        usual five when the page leaves the scale unstated.
    """
    return (
        normalize.parse_float(value(scope, "ratingValue")),
        normalize.parse_int(value(scope, "reviewCount")),
        normalize.parse_int(value(scope, "bestRating")) or DEFAULT_RATING_MAX,
    )
