from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

#: The four audiences one report has: the systemd journal, a chat window with an
#: assistant in it, a browser, and whatever else reads JSON.
FORMATS: Final[tuple[str, ...]] = ("text", "markdown", "json", "html")

#: Mirrors the default of :func:`coffee_aggregator.db.report.findings`. argparse
#: needs a literal of its own, and the two are meant to stay equal.
DEFAULT_HISTORY_DAYS: Final = 7

#: The severity that decides whether a morning needs anybody at all.
HIGH: Final = "high"

#: What an empty ``site`` means — the finding is about the catalogue as a whole.
CATALOGUE_WIDE: Final = "catalogue-wide"

#: What each kind means and where to look when it turns up. Both the markdown
#: report and the page are read by somebody who has never seen this catalogue —
#: an assistant asked to fix it, or whoever opens the page on a Monday — and a
#: slug on its own ("parse-gap") tells neither of them anything they can act on.
KIND_NOTES: Final[dict[str, str]] = {
    "no-products": (
        "The shop stored no product at all. That is a rotted selector, a shop that "
        "changed platform, or a shop that is gone — and it is one of the two things "
        "that make the nightly crawl exit 1. Start with "
        "`coffee-aggregator runs --site <shop> --limit 5` for the counters and the "
        "recorded errors, then save one listing page and re-run the discovery."
    ),
    "write-drop": (
        "The shop stored far fewer products than its own recent runs did. Pagination "
        "that changed shape, a listing that now needs JavaScript, and a run cut short "
        "by its deadline all look like this; `runs` tells them apart, because a "
        "truncated run is recorded with `complete: false`."
    ),
    "parse-gap": (
        "A field these pages used to yield is missing on many of them now. Save one "
        "product page and run `coffee-aggregator parse --site <shop> --file page.html` "
        "to see exactly what the parser gets. Measure the ceiling first: a shop that "
        "never published the datum is not a parsing bug."
    ),
    "weight-change": (
        "Stored weights moved on products that were already in the catalogue. Weight "
        "is the divisor of the price per kilogram, so a weight read off the wrong "
        "element turns into a price nobody can compare."
    ),
    "price-jump": (
        "Prices moved further overnight than this shop's prices normally move. A real "
        "sale looks exactly like a parser that picked up the wrong element; the "
        "product URLs in the detail are what tells the two apart."
    ),
    "new-failures": (
        "Requests or parses failed that were not failing before. Check for 429s and "
        "for a robots.txt that changed — a host that closed is not a bug to fix in "
        "the parser."
    ),
    "coverage-drop": (
        "Catalogue-wide: the share of products carrying a field fell. One large shop "
        "can move this number on its own, so read the per-shop numbers before "
        "changing anything shared."
    ),
}

#: Prefixes and suffixes that mark a detail key as the day-before twin of
#: another one. ``written`` beside ``previous_written`` is what lets the page
#: print a figure and the arrow next to it without the detection half having to
#: agree on a second shape first.
PREVIOUS_FORMS: Final[tuple[str, ...]] = ("previous_{key}", "prev_{key}", "{key}_before")


class FindingLike(Protocol):
    """The shape :mod:`coffee_aggregator.db.report` hands over.

    Structural rather than imported: rendering needs the five fields of the
    contract and nothing else, and reading the shape instead of the class is
    what lets the two halves of the report be built and tested apart.
    """

    @property
    def kind(self) -> str:
        """The stable slug naming what was found, such as ``no-products``."""
        ...

    @property
    def site(self) -> str:
        """The shop it is about, or ``""`` when it is about the catalogue."""
        ...

    @property
    def summary(self) -> str:
        """One line a human reads, with the numbers already in it."""
        ...

    @property
    def detail(self) -> Mapping[str, Any]:
        """The figures behind the summary, for a machine."""
        ...

    @property
    def severity(self) -> str:
        """``"high"`` when it needs somebody this morning, ``"low"`` otherwise."""
        ...


def split(found: Sequence[FindingLike]) -> tuple[list[FindingLike], list[FindingLike]]:
    """Divide findings into the ones that need somebody and the rest.

    Anything that is not :data:`HIGH` counts as low, so a severity this renderer
    has never heard of is shown rather than dropped.

    Args:
        found: The day's findings.

    Returns:
        The serious ones and the ones worth a look, each in the given order.
    """
    serious = [finding for finding in found if finding.severity == HIGH]
    return serious, [finding for finding in found if finding.severity != HIGH]


def headline(finding: FindingLike) -> str:
    """Render one finding as the single line the journal shows.

    Args:
        finding: The finding to describe.

    Returns:
        The kind, the shop and the summary.
    """
    # The detection half owns the wording of `summary` and is free to open it
    # with the shop's name; naming the shop twice reads as a stutter in the one
    # line most mornings consist of.
    if finding.site and not finding.summary.startswith(finding.site):
        return f"[{finding.kind}] {finding.site}: {finding.summary}"
    return f"[{finding.kind}] {finding.summary}"


def label(site: str) -> str:
    """Name the thing a finding is about.

    Args:
        site: A shop id, or ``""``.

    Returns:
        The shop id, or :data:`CATALOGUE_WIDE`.
    """
    return site or CATALOGUE_WIDE


def window(history_days: int) -> str:
    """Name the stretch of days the day was compared against.

    Args:
        history_days: How many days before it were read.

    Returns:
        A noun phrase that fits after "against".
    """
    if history_days == 1:
        return "the day before it"
    return f"the {history_days} days before it"


def is_scalar(value: object) -> bool:
    """Say whether a detail value fits in a table cell.

    Args:
        value: One value out of a ``detail`` mapping.

    Returns:
        True for anything that is not a list, tuple or mapping.
    """
    return not isinstance(value, list | tuple | dict)


def scalars(detail: Mapping[str, Any]) -> dict[str, Any]:
    """Return the detail entries that fit in a table.

    Args:
        detail: The figures behind one finding.

    Returns:
        The scalar entries, in the order the detection half wrote them.
    """
    return {key: value for key, value in detail.items() if is_scalar(value)}


def collections(detail: Mapping[str, Any]) -> dict[str, Any]:
    """Return the detail entries that do not fit in a table.

    Args:
        detail: The figures behind one finding.

    Returns:
        The list and mapping entries, in the order the detection half wrote them.
    """
    return {key: value for key, value in detail.items() if not is_scalar(value)}


def items(value: object) -> str:
    """Count the entries of a collection, for a label beside its key.

    Args:
        value: A list, tuple or mapping out of a ``detail`` mapping.

    Returns:
        A parenthesised count, or an empty string for a mapping.
    """
    if not isinstance(value, list | tuple):
        return ""
    return f" ({len(value)} item{'' if len(value) == 1 else 's'})"


def previous_of(detail: Mapping[str, Any], key: str) -> Any | None:  # noqa: ANN401
    """Find the day-before twin of a detail key, when there is one.

    Args:
        detail: The figures behind one finding.
        key: The key whose earlier value is wanted.

    Returns:
        The earlier value, or None when the detail carries no twin.
    """
    for form in PREVIOUS_FORMS:
        twin = form.format(key=key)
        if twin in detail and is_scalar(detail[twin]):
            return detail[twin]
    return None


def first_of(row: Mapping[str, Any], keys: Iterable[str]) -> Any | None:  # noqa: ANN401
    """Return the first of several candidate keys that the mapping carries.

    The contract fixes the five fields of a finding but says nothing about the
    keys inside ``detail``, so the page reads the names it knows and falls back
    to showing the raw detail for everything else.

    Args:
        row: A mapping out of a finding's detail.
        keys: Candidate key names, best first.

    Returns:
        The first value found, or None.
    """
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None
