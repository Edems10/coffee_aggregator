from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from coffee_aggregator import normalize

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "LABEL_RE",
    "LabelledLines",
    "absolute",
    "attr",
    "classes",
    "lines",
    "page_meta",
    "parse_label_lines",
    "text",
    "unique",
]

#: A ``LABEL: value`` line, as roasteries write them inside a description block.
#: The label is capped at 40 characters so an ordinary sentence with a colon in
#: it is read as prose rather than as a parameter.
LABEL_RE: Final = re.compile(r"^(?P<label>[^:]{2,40}?)\s*:\s*(?P<value>\S.*)$")

#: ``<meta>`` names and Open Graph properties worth keeping verbatim.
_META_NAMES: Final[tuple[tuple[str, str], ...]] = (
    ("META_DESCRIPTION", "description"),
    ("META_KEYWORDS", "keywords"),
)
_OG_PROPERTIES: Final[tuple[tuple[str, str], ...]] = (
    ("OG_TITLE", "og:title"),
    ("OG_DESCRIPTION", "og:description"),
)

_LINE_BREAK_RE: Final = re.compile(
    r"<br\s*/?>|</p\s*>|</div\s*>|</li\s*>|</tr\s*>|</h[1-6]\s*>",
    re.IGNORECASE,
)


def text(tag: Tag | None) -> str | None:
    """Return the visible text of a tag.

    Args:
        tag: The tag, or None when the selector matched nothing.

    Returns:
        The collapsed text, or None when the tag is missing or empty.
    """
    if tag is None:
        return None
    return tag.get_text(" ", strip=True) or None


def attr(tag: Tag | None, name: str) -> str | None:
    """Return one attribute of a tag as a plain string.

    Args:
        tag: The tag, or None when the selector matched nothing.
        name: The attribute name.

    Returns:
        The attribute value, or None when it is missing or empty.
    """
    if tag is None:
        return None
    value = tag.get(name)
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        return " ".join(str(item) for item in value).strip() or None
    return None


def classes(tag: Tag | None) -> list[str]:
    """Return the CSS classes of a tag.

    Args:
        tag: The tag, or None when the selector matched nothing.

    Returns:
        The class tokens, or an empty list.
    """
    if tag is None:
        return []
    value = tag.get("class")
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def lines(tag: Tag | None) -> list[str]:
    """Split a block into the lines a reader sees.

    Args:
        tag: The block, or None when it is absent from the page.

    Returns:
        One entry per non-empty line.
    """
    if tag is None:
        return []
    fragment = _LINE_BREAK_RE.sub("\n", tag.decode())
    body = BeautifulSoup(fragment, "lxml").get_text("")
    return [line.strip() for line in body.splitlines() if line.strip()]


def unique(values: Iterable[str | None]) -> list[str]:
    """Drop empty and duplicate values while keeping the original order.

    Args:
        values: The raw values.

    Returns:
        The cleaned list.
    """
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip() if value else ""
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def absolute(base_url: str, url: str | None) -> str | None:
    """Turn a shop-relative URL into an absolute one.

    Args:
        base_url: The shop's base URL.
        url: A href or src as written in the markup.

    Returns:
        The absolute URL, or None when the input was empty.
    """
    if not url:
        return None
    return urljoin(base_url, url.strip())


@dataclass(slots=True)
class LabelledLines:
    """The ``LABEL: value`` pairs of a text block, plus the prose around them."""

    pairs: list[tuple[str, str]] = field(default_factory=list)
    prose: list[str] = field(default_factory=list)

    def raw(self) -> dict[str, str]:
        """Return the labels as written, upper-cased, for ``raw_attributes``.

        Returns:
            Label -> value; the first value a label states wins.
        """
        collected: dict[str, str] = {}
        for label, value in self.pairs:
            collected.setdefault(label.upper(), value)
        return collected

    def folded(self) -> dict[str, str]:
        """Return the same pairs keyed by folded label, for field mapping.

        Returns:
            Folded label -> value; the first value a label states wins.
        """
        collected: dict[str, str] = {}
        for label, value in self.pairs:
            collected.setdefault(normalize.fold(label), value)
        return collected


def parse_label_lines(blocks: Iterable[Tag | None]) -> LabelledLines:
    """Split description blocks into labelled values and marketing prose.

    Line breaks are taken from the markup (``<br/>``, ``</p>``, ``</div>``)
    rather than from ``Tag.text``, which concatenates the strings around every
    ``<br/>`` with no separator — the bug that turned ``ZBER: …, 2026`` plus
    ``100 % Arabika`` into ``2026100 % Arabika``.

    Args:
        blocks: The blocks to read, in priority order; missing ones are skipped.

    Returns:
        Every pair the blocks state and every line that is not a pair.
    """
    parsed = LabelledLines()
    for block in blocks:
        for line in lines(block):
            match = LABEL_RE.match(line)
            if match is None:
                parsed.prose.append(line)
                continue
            parsed.pairs.append((match.group("label").strip(), match.group("value").strip()))
    return parsed


def page_meta(soup: BeautifulSoup) -> dict[str, str]:
    """Collect the page-level metadata every shop template carries.

    Open Graph and the classic ``<meta>`` tags routinely name the origin or the
    cup notes that the visible markup states nowhere else, so they are kept as
    ``raw_attributes`` even when nothing maps them to a typed field.

    Args:
        soup: The whole parsed page.

    Returns:
        Upper-cased keys mapped to the values the page states; absent tags are
        left out entirely.
    """
    collected: dict[str, str] = {}
    for key, name in _META_NAMES:
        value = attr(soup.select_one(f'meta[name="{name}"]'), "content")
        if value:
            collected[key] = value
    for key, prop in _OG_PROPERTIES:
        value = attr(soup.select_one(f'meta[property="{prop}"]'), "content")
        if value:
            collected[key] = value
    return collected
