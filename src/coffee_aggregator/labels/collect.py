from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from coffee_aggregator import normalize
from coffee_aggregator.labels.terms import F_COUNTRY, F_PROCESS, F_ROAST, F_SHIP_WEIGHT, F_WEIGHT
from coffee_aggregator.models import RoastLevel, RoastProfile

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "LABEL_RE",
    "MAX_BARE_LABEL_WORDS",
    "MAX_BARE_VALUE_CHARS",
    "MAX_BEAN_WEIGHT_G",
    "MAX_FUZZY_LABEL_CHARS",
    "MAX_FUZZY_LABEL_WORDS",
    "MIN_BEAN_WEIGHT_G",
    "Labels",
    "bare_label",
    "clean_label",
    "label_pair",
    "map_label",
    "plausible",
    "plausible_weight",
    "read_lines",
    "says",
]

#: A label longer than this is a sentence, not a parameter name.
MAX_FUZZY_LABEL_WORDS: Final = 4
MAX_FUZZY_LABEL_CHARS: Final = 30
#: Bean weights a coffee shop plausibly sells, in grams.
MIN_BEAN_WEIGHT_G: Final = 50
MAX_BEAN_WEIGHT_G: Final = 5000
#: A processing row states a method, not a paragraph.
_MAX_PROCESS_WORDS: Final = 6
#: A roast row states a level or a style, not a paragraph about the roastery.
_MAX_ROAST_WORDS: Final = 10

#: The trailing ``" :"`` and help ``"?"`` a shop puts in a header cell.
_LABEL_TRIM: Final = " \t:?"
#: The "✓"/"•"/"–" decoration a shop puts in front of its parameter names. It is
#: removed before matching so no shop has to spell the glyph into its label map.
_LABEL_DECORATION_RE: Final = re.compile(r"^[^0-9A-Za-zÀ-ɏ]+")

#: A ``LABEL: value`` line, as roasteries write them inside a description block.
#: The label is capped at 40 characters so an ordinary sentence with a colon in
#: it is read as prose rather than as a parameter.
#:
#: It is spelled here rather than imported from :mod:`coffee_aggregator.sites.html`
#: because this package reads strings and must stay free of ``bs4``;
#: ``tests/test_facts.py`` asserts the two patterns never drift apart.
LABEL_RE: Final = re.compile(r"^(?P<label>[^:]{2,40}?)\s*:\s*(?P<value>\S.*)$")

#: How many words a line may hold and still be read as a bare label whose value
#: is the line under it.
MAX_BARE_LABEL_WORDS: Final = 4
#: How long the line under a bare label may be and still be that label's value.
#: A roastery writes "Chuťový profil" over its cup notes and "Pôvod" over a
#: whole paragraph about the farm; only the first is a parameter value.
MAX_BARE_VALUE_CHARS: Final = 120


def clean_label(label: str | None) -> str:
    """Strip the punctuation and decoration a shop wraps a parameter name in.

    Args:
        label: The label as the shop wrote it.

    Returns:
        The bare parameter name, empty when nothing is left of it.
    """
    return _LABEL_DECORATION_RE.sub("", (label or "").strip(_LABEL_TRIM).strip()).strip()


def map_label(folded: str, label_map: dict[str, str]) -> str | None:
    """Resolve a folded label to a field: exactly first, then by whole words.

    A plain substring match is what let "Balení kávy značky Kávy pitel" claim the
    weight field and "Objednávky nad 1 kg zasíláme zdarma" claim it again. A
    match is therefore only attempted on a label short enough to be a parameter
    name rather than a sentence, and only on whole words.

    Args:
        folded: The folded label, e.g. ``"stupen prazeni"``.
        label_map: Folded label -> field.

    Returns:
        The field name, None when nothing matches, and ``""`` for labels that
        are deliberately ignored.
    """
    if folded in label_map:
        return label_map[folded]
    words = folded.split()
    if len(words) > MAX_FUZZY_LABEL_WORDS or len(folded) > MAX_FUZZY_LABEL_CHARS:
        return None
    for key in sorted(label_map, key=len, reverse=True):
        if says(words, key):
            return label_map[key]
    return None


def says(words: list[str], key: str) -> bool:
    """Say whether a label contains a key as a run of whole words.

    Args:
        words: The folded label, already split on whitespace.
        key: A folded key of the label map.

    Returns:
        True when the key's words appear consecutively in the label.
    """
    wanted = key.split()
    if not wanted or len(wanted) > len(words):
        return False
    return any(
        words[start : start + len(wanted)] == wanted
        for start in range(len(words) - len(wanted) + 1)
    )


def plausible_weight(grams: int | None) -> bool:
    """Say whether a gram count is a bag of coffee rather than a pack code.

    Args:
        grams: The weight a source stated, or None when it stated none.

    Returns:
        True when the weight is inside the range a roastery actually sells.
    """
    return grams is not None and MIN_BEAN_WEIGHT_G <= grams <= MAX_BEAN_WEIGHT_G


def plausible(field_name: str, value: str) -> bool:
    """Reject a value that cannot be what the field it was mapped onto means.

    The label alone is never proof: "Popis zpracování objednávky" reads like a
    processing row and holds a paragraph about shipping. A field that has an
    obvious shape is therefore checked against it, and a value that fails stays
    in ``raw_attributes`` only.

    Args:
        field_name: One of the ``F_*`` constants.
        value: The value the shop wrote.

    Returns:
        True when the value may feed that field.
    """
    if field_name in {F_WEIGHT, F_SHIP_WEIGHT}:
        return plausible_weight(normalize.parse_weight_grams(value))
    if field_name == F_PROCESS:
        return len(value.split()) <= _MAX_PROCESS_WORDS
    if field_name == F_COUNTRY:
        return normalize.detect_country(value) is not None
    if field_name == F_ROAST:
        return _plausible_roast(value)
    return True


def _plausible_roast(value: str) -> bool:
    """Say whether a value can be the roast the label promised.

    A roast row states a level ("stredne tmavé"), a style ("Espresso") or both.
    Anything that names neither is a row some other label claimed — "Druh: Směs
    Arabiky a Robusty" under a fuzzy "pražení" match — and believing it stores a
    dark roast for a light coffee, which is the one field a drinker sorts on.

    Args:
        value: The value the shop wrote.

    Returns:
        True when the value names a roast level or a roast profile.
    """
    if len(value.split()) > _MAX_ROAST_WORDS:
        return False
    return (
        normalize.normalize_roast_level(value) is not RoastLevel.UNKNOWN
        or normalize.normalize_roast_profile(value) is not RoastProfile.UNKNOWN
    )


def label_pair(line: str) -> list[tuple[str, str]]:
    """Read the one ``Label: value`` an ordinary description line states.

    Args:
        line: One line of a rendered description block.

    Returns:
        A one-element list, or an empty one when the line is prose.
    """
    match = LABEL_RE.match(line)
    if match is None:
        return []
    return [(match.group("label").strip(), match.group("value").strip())]


def bare_label(line: str, label_map: dict[str, str]) -> bool:
    """Say whether a whole line is a label whose value is the line under it.

    ``<p><strong>Chuťový profil</strong></p><p>Jablko, maliny…</p>`` is how a
    roastery writes a heading and its value; read line by line the heading
    states no value at all and the notes state no label. The same shape arrives
    as ``<div>Pražení:</div><div>Světlé</div>`` and as ``<p>Pražení:<br/>
    Světlé</p>``, which is why the trailing colon is stripped first.

    Args:
        line: One line of the rendered description.
        label_map: Folded label -> field.

    Returns:
        True when the line names a field and nothing else.
    """
    cleaned = clean_label(line)
    if not cleaned or len(cleaned.split()) > MAX_BARE_LABEL_WORDS:
        return False
    return bool(map_label(normalize.fold(cleaned), label_map))


def read_lines(
    lines: Sequence[str],
    label_map: dict[str, str],
    pairs_of: Callable[[str, dict[str, str]], list[tuple[str, str]]] | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Split rendered description lines into labelled values and prose.

    Every platform hit the same wall: a shop states five different label shapes
    and only the one that keeps the label and its value on a single line was
    read. A label standing alone on its own line is the other four at once —
    ``<div>``/``<div>``, ``<p>``/``<p>``, ``<p>…<br/>…</p>`` and a bare heading
    over its value — so the pair is taken from the line that follows.

    Args:
        lines: The rendered lines of the description blocks, in page order.
        label_map: Folded label -> field, which is what keeps an ordinary
            sentence from claiming the line under it.
        pairs_of: A platform's own single-line reader, when it states more
            shapes than ``LABEL: value``; :func:`label_pair` by default.

    Returns:
        Every pair the lines state, and every line that is not one.
    """
    read = pairs_of if pairs_of is not None else lambda line, _map: label_pair(line)
    pairs: list[tuple[str, str]] = []
    prose: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        found = read(line, label_map)
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if found:
            pairs.extend(found)
        elif bare_label(line, label_map) and _is_bare_value(following, label_map, read):
            pairs.append((line, following))
            index += 2
            continue
        else:
            prose.append(line)
        index += 1
    return pairs, prose


def _is_bare_value(
    line: str,
    label_map: dict[str, str],
    read: Callable[[str, dict[str, str]], list[tuple[str, str]]],
) -> bool:
    """Say whether a line is the value belonging to the label above it.

    A line that states its own label is the next fact, not this one's value:
    swallowing it cost ``longberry`` its region and ``riksakava`` its roast,
    because both shops print one heading above a whole run of labelled lines.

    Args:
        line: The line under the bare label.
        label_map: Folded label -> field.
        read: The single-line pair reader in force.

    Returns:
        True when the line is a plain value short enough to be one.
    """
    if not 0 < len(line) <= MAX_BARE_VALUE_CHARS:
        return False
    return not read(line, label_map) and not bare_label(line, label_map)


@dataclass(slots=True)
class Labels:
    """Every labelled value a product page states.

    Attributes:
        raw: Labels as written (trimmed, upper-cased) -> value, for the sink.
        by_field: Canonical field name -> the first value that fed it.
    """

    raw: dict[str, str] = field(default_factory=dict)
    by_field: dict[str, str] = field(default_factory=dict)

    def add(self, label: str | None, value: str | None, label_map: dict[str, str]) -> None:
        """Record one ``label: value`` pair.

        Args:
            label: The label as the shop wrote it.
            value: The value as the shop wrote it.
            label_map: Folded label -> field, already merged with the defaults.
        """
        cleaned = clean_label(label)
        text = (value or "").strip()
        if not cleaned or not text:
            return
        self.raw.setdefault(cleaned.upper(), text)
        mapped = map_label(normalize.fold(cleaned), label_map)
        if mapped and plausible(mapped, text):
            self.by_field.setdefault(mapped, text)

    def get(self, field_name: str) -> str | None:
        """Return the value mapped onto one field.

        Args:
            field_name: One of the ``F_*`` constants.

        Returns:
            The value, or None when no label fed that field.
        """
        return self.by_field.get(field_name)
