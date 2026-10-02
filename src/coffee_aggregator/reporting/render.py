from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from coffee_aggregator.reporting.finding import (
    DEFAULT_HISTORY_DAYS,
    FORMATS,
    HIGH,
    KIND_NOTES,
    FindingLike,
    collections,
    headline,
    items,
    label,
    scalars,
    split,
    window,
)
from coffee_aggregator.reporting.page import document

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

#: How many low-severity findings ``text`` prints before it stops naming them.
#: That format is read at a glance in the journal: a summary that scrolls is a
#: summary nobody finishes, and the other three carry every one of them.
TEXT_LOW_LIMIT: Final = 5


def render(
    found: Sequence[FindingLike],
    *,
    day: date,
    history_days: int = DEFAULT_HISTORY_DAYS,
    fmt: str = "text",
) -> str:
    """Render one day's findings for one audience.

    Args:
        found: What the detection half returned, most severe first.
        day: The crawl day being reported on.
        history_days: How many days before it that day was compared against.
        fmt: One of :data:`~coffee_aggregator.reporting.finding.FORMATS`.

    Returns:
        The finished report, ending in a newline.

    Raises:
        ValueError: When ``fmt`` is not a format this module renders.
    """
    if fmt == "text":
        return _text(found, day=day)
    if fmt == "markdown":
        return _markdown(found, day=day, history_days=history_days)
    if fmt == "json":
        return _json(found)
    if fmt == "html":
        return document(found, day=day, history_days=history_days)
    message = f"unknown report format {fmt!r}; expected one of {', '.join(FORMATS)}"
    raise ValueError(message)


def verdict(day: date, serious: Sequence[FindingLike], minor: Sequence[FindingLike]) -> str:
    """Render the line that answers "was last night fine?".

    Args:
        day: The crawl day being reported on.
        serious: The high-severity findings.
        minor: Everything else.

    Returns:
        One line, leading with the day.
    """
    if not serious and not minor:
        return f"report {day.isoformat()}: nothing to report"
    if not serious:
        return f"report {day.isoformat()}: nothing serious, {len(minor)} worth a look"
    if not minor:
        return f"report {day.isoformat()}: {len(serious)} serious"
    return f"report {day.isoformat()}: {len(serious)} serious, {len(minor)} worth a look"


def _text(found: Sequence[FindingLike], *, day: date) -> str:
    """Render the report the systemd journal gets.

    Args:
        found: The day's findings.
        day: The crawl day being reported on.

    Returns:
        The verdict, then every serious finding, then a capped list of the rest.
    """
    serious, minor = split(found)
    lines = [verdict(day, serious, minor)]
    if serious:
        lines.append("serious:")
        lines.extend(f"  {headline(finding)}" for finding in serious)
    if minor:
        lines.append("worth a look:")
        lines.extend(f"  {headline(finding)}" for finding in minor[:TEXT_LOW_LIMIT])
        hidden = len(minor) - TEXT_LOW_LIMIT
        if hidden > 0:
            lines.append(f"  … and {hidden} more; see 'report --format markdown'")
    return "\n".join(lines) + "\n"


def _json(found: Sequence[FindingLike]) -> str:
    """Render the findings as the data they are.

    The five fields are named here rather than taken off the dataclass: they are
    the contract this module renders, and what a caller parses should not change
    shape because the detection half grew a field.

    Args:
        found: The day's findings.

    Returns:
        A JSON array of objects, one per finding.
    """
    payload = [
        {
            "kind": finding.kind,
            "site": finding.site,
            "summary": finding.summary,
            "detail": dict(finding.detail),
            "severity": finding.severity,
        }
        for finding in found
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"


def _cell(value: Any) -> str:  # noqa: ANN401  (a detail value is whatever was measured)
    """Render one detail value for a markdown table cell.

    Args:
        value: One scalar out of a ``detail`` mapping.

    Returns:
        The value as JSON inside a code span, unambiguous about type and quoting.
    """
    text = json.dumps(value, ensure_ascii=False, default=str)
    return "`" + text.replace("|", "\\|") + "`"


def _detail_markdown(detail: Mapping[str, Any]) -> list[str]:
    """Render one finding's figures in full.

    Scalars go in a table and anything larger in a fenced JSON block: the reader
    is an assistant being asked to fix what the report found, and a hundred-row
    table of product URLs helps it less than the list it can parse.

    Args:
        detail: The figures behind one finding.

    Returns:
        Markdown lines, empty when the finding carries no detail.
    """
    lines: list[str] = []
    flat = scalars(detail)
    if flat:
        lines += ["| field | value |", "| --- | --- |"]
        lines += [f"| `{key}` | {_cell(value)} |" for key, value in flat.items()]
        lines.append("")
    for key, value in collections(detail).items():
        body = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        lines += [f"`{key}`{items(value)}:", "", "```json", body, "```", ""]
    return lines


def _per_shop_markdown(found: Sequence[FindingLike]) -> list[str]:
    """Render the table that says which shops the night went wrong at.

    Args:
        found: The day's findings.

    Returns:
        Markdown lines for a table, worst shop first.
    """
    tally: dict[str, list[int]] = {}
    kinds: dict[str, list[str]] = {}
    for finding in found:
        name = label(finding.site)
        counts = tally.setdefault(name, [0, 0])
        counts[0 if finding.severity == HIGH else 1] += 1
        if finding.kind not in kinds.setdefault(name, []):
            kinds[name].append(finding.kind)
    rows = sorted(tally.items(), key=lambda item: (-item[1][0], -item[1][1], item[0]))
    lines = [
        "## Findings per shop",
        "",
        "| shop | serious | worth a look | kinds |",
        "| --- | --: | --: | --- |",
    ]
    for name, counts in rows:
        slugs = ", ".join(f"`{kind}`" for kind in kinds[name])
        lines.append(f"| `{name}` | {counts[0]} | {counts[1]} | {slugs} |")
    lines.append("")
    return lines


def _section_markdown(title: str, group: Sequence[FindingLike]) -> list[str]:
    """Render one severity's findings.

    Args:
        title: The heading for the group.
        group: The findings in it, in the order they arrived.

    Returns:
        Markdown lines, empty when the group is.
    """
    if not group:
        return []
    lines = [f"## {title}", ""]
    for finding in group:
        lines += [f"### {label(finding.site)} — `{finding.kind}`", "", finding.summary, ""]
        lines += _detail_markdown(finding.detail)
    return lines


def _notes_markdown(found: Sequence[FindingLike]) -> list[str]:
    """Explain the kinds this report actually contains.

    Args:
        found: The day's findings.

    Returns:
        Markdown lines, empty when no kind present is one this module knows.
    """
    seen: list[str] = []
    for finding in found:
        if finding.kind in KIND_NOTES and finding.kind not in seen:
            seen.append(finding.kind)
    if not seen:
        return []
    lines = ["## What these kinds mean", ""]
    lines += [f"**`{kind}`** — {KIND_NOTES[kind]}\n" for kind in seen]
    return lines


def _markdown(found: Sequence[FindingLike], *, day: date, history_days: int) -> str:
    """Render the report that is pasted into a chat.

    Args:
        found: The day's findings.
        day: The crawl day being reported on.
        history_days: How many days before it that day was compared against.

    Returns:
        A document that stands on its own, for a reader who has never seen this
        catalogue.
    """
    stamp = day.isoformat()
    title = f"# Crawl report — {stamp}"
    against = window(history_days)
    if not found:
        return f"{title}\n\nNothing to report: the crawl of {stamp} looks like {against}.\n"

    serious, minor = split(found)
    lines = [
        title,
        "",
        (
            f"{len(found)} findings from the crawl of {stamp}, measured against {against}: "
            f"{len(serious)} serious, {len(minor)} worth a look."
        ),
        "",
        (
            "Every figure below was measured from that day's `crawl_run` rows and the "
            "products stored against them; none of it is an estimate. "
            "`coffee-aggregator runs --site <shop> --limit 5` prints the counters behind "
            f"any one shop, and `coffee-aggregator report --day {stamp} --format json` "
            "prints this same report as data."
        ),
        "",
    ]
    lines += _per_shop_markdown(found)
    lines += _section_markdown("Serious", serious)
    lines += _section_markdown("Worth a look", minor)
    lines += _notes_markdown(found)
    return "\n".join(lines).rstrip("\n") + "\n"
