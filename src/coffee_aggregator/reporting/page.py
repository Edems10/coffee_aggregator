from __future__ import annotations

import html
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from coffee_aggregator.reporting.finding import (
    HIGH,
    KIND_NOTES,
    FindingLike,
    collections,
    first_of,
    items,
    label,
    previous_of,
    scalars,
    split,
    window,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

#: The kind whose detail fills the price table.
PRICE_KIND: Final = "price-jump"

#: The kind that says a product's stored weight moved. A product in both is not
#: a price move at all: the shop's headline variant changed size, so yesterday's
#: price and today's buy different things.
WEIGHT_KIND: Final = "weight-change"

#: Candidate detail keys, best first. The contract fixes the five fields of a
#: finding and says nothing about what goes inside ``detail``, so the page reads
#: the names it knows and falls back to printing the raw detail for the rest.
ROW_KEYS: Final[tuple[str, ...]] = ("products", "movers", "changes", "items", "rows")
PRODUCT_KEYS: Final[tuple[str, ...]] = ("product", "name", "title", "product_name")
BEFORE_KEYS: Final[tuple[str, ...]] = (
    "previous_price",
    "price_before",
    "prev_price",
    "before",
    "was",
    "old_price",
    "from",
)
AFTER_KEYS: Final[tuple[str, ...]] = (
    "price",
    "current_price",
    "new_price",
    "after",
    "now",
    "to",
)
PCT_KEYS: Final[tuple[str, ...]] = ("change_pct", "pct_change", "change_percent", "percent", "pct")
PER_KG_KEYS: Final[tuple[str, ...]] = (
    "price_per_kg",
    "per_kg",
    "price_per_kilogram",
    "czk_per_kg",
    "eur_per_kg",
)
PREVIOUS_PER_KG_KEYS: Final[tuple[str, ...]] = (
    "previous_price_per_kg",
    "price_per_kg_before",
    "prev_price_per_kg",
    "previous_per_kg",
)
WEIGHT_FLAG_KEYS: Final[tuple[str, ...]] = ("weight_changed", "variant_changed", "weight_change")
URL_KEYS: Final[tuple[str, ...]] = ("url", "product_url", "link")
CURRENCY_KEYS: Final[tuple[str, ...]] = ("currency", "price_currency")

#: One file, no network. The page is written to disk by the nightly run and
#: served as a static file from a directory, so a webfont or a CDN stylesheet
#: would be a request that fails on a `file://` path and a dependency on
#: somebody else's uptime for a report about last night.
STYLE: Final = """
:root {
  color-scheme: light dark;
  --bg: #f5f4f1;
  --surface: #ffffff;
  --border: #e3dfd8;
  --text: #1b1a18;
  --muted: #6c6760;
  --good: #1d6f48;
  --good-bg: #e6f2ea;
  --warn: #7f5a00;
  --warn-bg: #fbf0d9;
  --bad: #9d2a21;
  --bad-bg: #fae9e6;
  --accent: #35527f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15151a;
    --surface: #1e1e24;
    --border: #32323b;
    --text: #eceae6;
    --muted: #a19d96;
    --good: #5fd09a;
    --good-bg: #122c1e;
    --warn: #eac05c;
    --warn-bg: #302512;
    --bad: #ff8d7d;
    --bad-bg: #371c19;
    --accent: #93b4e8;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
main { max-width: 60rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
h1 { font-size: 1.5rem; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 1.05rem; margin: 2.5rem 0 0.75rem; letter-spacing: 0.04em;
     text-transform: uppercase; color: var(--muted); }
h3 { font-size: 1rem; margin: 0 0 0.25rem; }
p { margin: 0.4rem 0; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
code { font-size: 0.9em; }
.masthead { color: var(--muted); font-size: 0.85rem; letter-spacing: 0.08em;
            text-transform: uppercase; margin-bottom: 0.2rem; }
.verdict { margin-top: 1.5rem; padding: 1.5rem 1.25rem; border-radius: 0.6rem;
           border: 1px solid var(--border); background: var(--surface); }
.verdict strong { display: block; font-size: clamp(1.6rem, 6vw, 2.4rem); line-height: 1.15;
                  letter-spacing: -0.02em; }
.verdict p { color: var(--muted); }
.verdict.good { border-color: var(--good); background: var(--good-bg); }
.verdict.good strong { color: var(--good); }
.verdict.warn { border-color: var(--warn); background: var(--warn-bg); }
.verdict.warn strong { color: var(--warn); }
.verdict.bad { border-color: var(--bad); background: var(--bad-bg); }
.verdict.bad strong { color: var(--bad); }
.tiles { display: grid; gap: 0.75rem; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 0.5rem;
        padding: 0.8rem 0.9rem; }
.tile .k { color: var(--muted); font-size: 0.8rem; }
.tile .v { font-size: 1.4rem; font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { width: 100%; border-collapse: collapse; background: var(--surface);
        border: 1px solid var(--border); border-radius: 0.5rem; }
th, td { padding: 0.5rem 0.7rem; border-bottom: 1px solid var(--border); text-align: left;
         vertical-align: top; }
th { font-size: 0.78rem; letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted);
     white-space: nowrap; }
tr:last-child td { border-bottom: none; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tr.flagged { background: var(--warn-bg); }
.up { color: var(--bad); }
.down { color: var(--good); }
.card { background: var(--surface); border: 1px solid var(--border); border-left-width: 4px;
        border-radius: 0.5rem; padding: 0.9rem 1rem; margin-bottom: 0.75rem; }
.card.high { border-left-color: var(--bad); }
.card.low { border-left-color: var(--warn); }
.card table { border: none; margin-top: 0.6rem; }
.kind { display: inline-block; font-size: 0.75rem; letter-spacing: 0.04em; padding: 0.1rem 0.4rem;
        border-radius: 0.25rem; background: var(--bg); border: 1px solid var(--border);
        color: var(--muted); }
.note { color: var(--muted); font-size: 0.9rem; }
details { margin-top: 0.5rem; }
summary { cursor: pointer; color: var(--accent); font-size: 0.9rem; }
pre { overflow-x: auto; background: var(--bg); border: 1px solid var(--border);
      border-radius: 0.4rem; padding: 0.6rem 0.7rem; font-size: 0.82rem; }
footer { margin-top: 3rem; color: var(--muted); font-size: 0.85rem; }
@media (max-width: 30rem) {
  main { padding: 1rem 0.75rem 3rem; }
  th, td { padding: 0.45rem 0.5rem; }
}
"""


@dataclass(frozen=True, slots=True)
class Mover:
    """One product whose price moved between the two days.

    Attributes:
        site: The shop it belongs to.
        product: Its name, as the shop publishes it.
        url: Its page, when the detail carried one.
        before: The price the day before.
        after: The price on the reported day.
        pct: The move as a percentage, negative when the price fell.
        per_kg: Today's price per kilogram, the comparable figure.
        previous_per_kg: The day before's, when the detail carried one.
        currency: The currency both prices are in, when the detail said.
        weight_changed: True when the product's weight moved as well, which
            means the two prices do not buy the same thing.
    """

    site: str
    product: str
    url: str
    before: Any
    after: Any
    pct: float | None
    per_kg: Any
    previous_per_kg: Any
    currency: str
    weight_changed: bool


def esc(value: object) -> str:
    """Make any value safe to drop into markup.

    Product names in this catalogue carry quotes, ampersands and angle brackets,
    and the page is written from the database straight to a served file.

    Args:
        value: Anything at all.

    Returns:
        Its text, with the five markup characters escaped.
    """
    return html.escape(str(value), quote=True)


def rich(text: str) -> str:
    """Escape prose and turn its backtick spans into code.

    The kind notes are written once and read in three places; markdown and the
    journal want the backticks, and on the page a command in the middle of a
    sentence reads better set in the monospace it will be typed in.

    Args:
        text: One sentence of prose, possibly with ``backtick`` spans in it.

    Returns:
        Escaped markup.
    """
    return "".join(
        f"<code>{esc(part)}</code>" if index % 2 else esc(part)
        for index, part in enumerate(text.split("`"))
    )


def _value(value: object) -> str:
    """Render one scalar detail value for a cell.

    Args:
        value: One scalar out of a ``detail`` mapping.

    Returns:
        The value as a reader expects it, booleans and nulls as JSON spells them.
    """
    if isinstance(value, str):
        return esc(value)
    return esc(json.dumps(value, ensure_ascii=False, default=str))


def _decimal(value: object) -> Decimal | None:
    """Read a detail value as a number when it is one.

    Args:
        value: One scalar out of a ``detail`` mapping.

    Returns:
        The number, or None when the value is not numeric.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation, ValueError:
        return None


def _amount(value: object, currency: str = "") -> str:
    """Render a price the way a reader scans it.

    Args:
        value: The figure, numeric or not.
        currency: Appended when the detail named one.

    Returns:
        The escaped figure, or an em dash when there is none.
    """
    if value is None or value == "":
        return "—"
    number = _decimal(value)
    if number is None:
        return esc(value)
    quantised = number.quantize(Decimal(1)) if number == number.to_integral_value() else number
    tail = f" {esc(currency)}" if currency else ""
    return f"{esc(quantised)}{tail}"


def _percent(pct: float | None) -> str:
    """Render a price move with its direction.

    Args:
        pct: The move as a percentage, or None when it could not be worked out.

    Returns:
        A signed percentage in a span classed by direction.
    """
    if pct is None:
        return "—"
    direction = "up" if pct > 0 else "down"
    return f'<span class="{direction}">{pct:+.1f}%</span>'


def _rows_of(detail: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Find the per-product rows inside one finding's detail.

    Args:
        detail: The figures behind one finding.

    Returns:
        The rows, or an empty list when the detail holds no list of mappings.
    """
    candidates = [detail[key] for key in ROW_KEYS if key in detail]
    candidates += [value for key, value in collections(detail).items() if key not in ROW_KEYS]
    for value in candidates:
        if not isinstance(value, list | tuple) or not value:
            continue
        if all(isinstance(row, dict) for row in value):
            return list(value)
    return []


def _weight_changed(found: Sequence[FindingLike]) -> set[tuple[str, str]]:
    """Collect every product whose stored weight moved as well.

    Args:
        found: The day's findings.

    Returns:
        ``(site, product)`` pairs named by the ``weight-change`` findings.
    """
    moved: set[tuple[str, str]] = set()
    for finding in found:
        if finding.kind != WEIGHT_KIND:
            continue
        rows = _rows_of(finding.detail) or [finding.detail]
        for row in rows:
            name = first_of(row, PRODUCT_KEYS)
            if name is not None:
                moved.add((finding.site, str(name)))
    return moved


def _mover(site: str, row: Mapping[str, Any], *, flagged: bool) -> Mover | None:
    """Build one table row out of one product's figures.

    Args:
        site: The shop the finding belongs to.
        row: One product's entry in the detail.
        flagged: Whether a ``weight-change`` finding names the same product.

    Returns:
        The row, or None when the detail holds neither price.
    """
    before = first_of(row, BEFORE_KEYS)
    after = first_of(row, AFTER_KEYS)
    if before is None and after is None:
        return None
    pct = _decimal(first_of(row, PCT_KEYS))
    old, new = _decimal(before), _decimal(after)
    # A percentage the detection half already worked out wins; this is only for
    # the detail that carries two prices and leaves the arithmetic to the reader.
    if pct is None and new is not None and old is not None and old != 0:
        pct = (new - old) / old * 100
    weight_flag = first_of(row, WEIGHT_FLAG_KEYS)
    return Mover(
        site=site,
        product=str(first_of(row, PRODUCT_KEYS) or "—"),
        url=str(first_of(row, URL_KEYS) or ""),
        before=before,
        after=after,
        pct=None if pct is None else float(pct),
        per_kg=first_of(row, PER_KG_KEYS),
        previous_per_kg=first_of(row, PREVIOUS_PER_KG_KEYS),
        currency=str(first_of(row, CURRENCY_KEYS) or ""),
        weight_changed=flagged or bool(weight_flag),
    )


def movers(found: Sequence[FindingLike]) -> list[Mover]:
    """Collect every product whose price moved, biggest move first.

    Args:
        found: The day's findings.

    Returns:
        The rows of the price table, sorted by the size of the move.
    """
    flagged = _weight_changed(found)
    rows: list[Mover] = []
    for finding in found:
        if finding.kind != PRICE_KIND:
            continue
        for row in _rows_of(finding.detail) or [finding.detail]:
            name = first_of(row, PRODUCT_KEYS)
            mover = _mover(
                finding.site, row, flagged=(finding.site, str(name)) in flagged if name else False
            )
            if mover is not None:
                rows.append(mover)
    rows.sort(key=lambda mover: abs(mover.pct or 0.0), reverse=True)
    return rows


def _price_table(rows: Sequence[Mover]) -> list[str]:
    """Render the price movers.

    Args:
        rows: The movers, biggest move first.

    Returns:
        Markup lines, empty when no price moved.
    """
    if not rows:
        return []
    out = [
        "<h2>Price movers</h2>",
        '<div class="scroll"><table>',
        (
            "<thead><tr><th>Shop</th><th>Product</th><th class='n'>Before</th>"
            "<th class='n'>Today</th><th class='n'>Change</th>"
            "<th class='n'>Per kg</th></tr></thead>"
        ),
        "<tbody>",
    ]
    for row in rows:
        name = f'<a href="{esc(row.url)}">{esc(row.product)}</a>' if row.url else esc(row.product)
        mark = ' <span class="kind">weight changed</span>' if row.weight_changed else ""
        per_kg = _amount(row.per_kg, row.currency)
        if row.previous_per_kg is not None:
            per_kg = f"{_amount(row.previous_per_kg)} → {per_kg}"
        out.append(
            ('<tr class="flagged">' if row.weight_changed else "<tr>")
            + f"<td><code>{esc(row.site)}</code></td>"
            f"<td>{name}{mark}</td>"
            f'<td class="n">{_amount(row.before, row.currency)}</td>'
            f'<td class="n">{_amount(row.after, row.currency)}</td>'
            f'<td class="n">{_percent(row.pct)}</td>'
            f'<td class="n">{per_kg}</td></tr>'
        )
    out += ["</tbody></table></div>"]
    if any(row.weight_changed for row in rows):
        out.append(
            '<p class="note">Rows marked <span class="kind">weight changed</span> are not price '
            "moves. The shop's headline variant changed size, so the two prices buy different "
            "amounts of coffee; the per-kilogram figure is the only comparable one there.</p>"
        )
    return out


def _tiles(found: Sequence[FindingLike]) -> list[str]:
    """Render the catalogue-wide totals and how they moved.

    A figure earns a tile when the detail also carries its day-before twin —
    a total with nothing to compare it against is a number, not a report.

    Args:
        found: The day's findings.

    Returns:
        Markup lines, empty when nothing catalogue-wide was measured twice.
    """
    cells: list[str] = []
    for finding in found:
        if finding.site:
            continue
        for key, value in scalars(finding.detail).items():
            earlier = previous_of(finding.detail, key)
            if earlier is None:
                continue
            now, then = _decimal(value), _decimal(earlier)
            move = "" if now is None or then is None else f"{now - then:+}"
            arrow = f'<div class="k">{esc(move)} vs {esc(earlier)}</div>' if move else ""
            cells.append(
                f'<div class="tile"><div class="k">{esc(key.replace("_", " "))}</div>'
                f'<div class="v">{_value(value)}</div>{arrow}</div>'
            )
    if not cells:
        return []
    return ["<h2>Catalogue</h2>", '<div class="tiles">', *cells, "</div>"]


def _per_shop(found: Sequence[FindingLike]) -> list[str]:
    """Render the table that says which shops the night went wrong at.

    Args:
        found: The day's findings.

    Returns:
        Markup lines for a table, worst shop first.
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
    out = [
        "<h2>Findings per shop</h2>",
        '<div class="scroll"><table>',
        (
            "<thead><tr><th>Shop</th><th class='n'>Serious</th>"
            "<th class='n'>Worth a look</th><th>Kinds</th></tr></thead><tbody>"
        ),
    ]
    out += [
        f"<tr><td><code>{esc(name)}</code></td>"
        f'<td class="n">{counts[0]}</td><td class="n">{counts[1]}</td>'
        f"<td>{', '.join(f'<code>{esc(kind)}</code>' for kind in kinds[name])}</td></tr>"
        for name, counts in rows
    ]
    out += ["</tbody></table></div>"]
    return out


def _detail_markup(detail: Mapping[str, Any]) -> list[str]:
    """Render one finding's figures.

    Args:
        detail: The figures behind one finding.

    Returns:
        Markup lines, empty when the finding carries no detail.
    """
    out: list[str] = []
    flat = scalars(detail)
    if flat:
        out.append('<div class="scroll"><table><tbody>')
        out += [
            f"<tr><td><code>{esc(key)}</code></td><td class='n'>{_value(value)}</td></tr>"
            for key, value in flat.items()
        ]
        out.append("</tbody></table></div>")
    for key, value in collections(detail).items():
        body = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        out.append(
            f"<details><summary><code>{esc(key)}</code>{esc(items(value))}</summary>"
            f"<pre>{esc(body)}</pre></details>"
        )
    return out


def _cards(title: str, group: Sequence[FindingLike], css: str) -> list[str]:
    """Render one severity's findings.

    Args:
        title: The heading for the group.
        group: The findings in it, in the order they arrived.
        css: The class that colours the group's left edge.

    Returns:
        Markup lines, empty when the group is.
    """
    if not group:
        return []
    out = [f"<h2>{esc(title)}</h2>"]
    for finding in group:
        out.append(f'<div class="card {css}">')
        out.append(
            f'<h3>{esc(label(finding.site))} <span class="kind">{esc(finding.kind)}</span></h3>'
        )
        out.append(f"<p>{esc(finding.summary)}</p>")
        note = KIND_NOTES.get(finding.kind)
        if note:
            out.append(f'<p class="note">{rich(note)}</p>')
        out += _detail_markup(finding.detail)
        out.append("</div>")
    return out


def _verdict(found: Sequence[FindingLike], *, day: date, history_days: int) -> list[str]:
    """Render the banner that answers the question the page exists for.

    Args:
        found: The day's findings.
        day: The crawl day being reported on.
        history_days: How many days before it that day was compared against.

    Returns:
        Markup lines for one banner.
    """
    stamp = esc(day.isoformat())
    against = esc(window(history_days))
    serious, minor = split(found)
    if not found:
        headline, tone = "Nothing to report", "good"
        note = f"The crawl of {stamp} looks like {against}."
    elif not serious:
        headline, tone = f"{len(minor)} worth a look", "warn"
        note = f"Nothing serious in the crawl of {stamp}, measured against {against}."
    else:
        headline, tone = f"{len(serious)} serious", "bad"
        tail = f" {len(minor)} more are worth a look." if minor else ""
        note = f"In the crawl of {stamp}, measured against {against}.{tail}"
    return [f'<section class="verdict {tone}"><strong>{headline}</strong><p>{note}</p></section>']


def document(
    found: Sequence[FindingLike],
    *,
    day: date,
    history_days: int,
) -> str:
    """Render the whole report as one standalone HTML document.

    Nothing is loaded from the network: the nightly run writes this file to a
    directory that nginx serves, and it has to render the same from a ``file://``
    path on a laptop with no connection.

    Args:
        found: The day's findings, most severe first.
        day: The crawl day being reported on.
        history_days: How many days before it that day was compared against.

    Returns:
        A complete document, ending in a newline.
    """
    stamp = esc(day.isoformat())
    serious, minor = split(found)
    body = [
        '<p class="masthead">Coffee aggregator — crawl report</p>',
        f"<h1>{stamp}</h1>",
        *_verdict(found, day=day, history_days=history_days),
    ]
    if found:
        body += _tiles(found)
        body += _price_table(movers(found))
        body += _per_shop(found)
        body += _cards("Serious", serious, "high")
        body += _cards("Worth a look", minor, "low")
    body.append(
        f"<footer>Written by <code>coffee-aggregator report --day {stamp} --format html</code>. "
        f"Run history lives in <code>crawl_run</code>; <code>coffee-aggregator runs --site "
        f"&lt;shop&gt;</code> prints the counters behind any one shop.</footer>"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Crawl report — {stamp}</title>\n"
        f"<style>{STYLE}</style>\n"
        "</head>\n"
        "<body>\n<main>\n" + "\n".join(body) + "\n</main>\n</body>\n</html>\n"
    )
