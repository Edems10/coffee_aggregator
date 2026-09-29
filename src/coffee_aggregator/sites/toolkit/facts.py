from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from coffee_aggregator import normalize
from coffee_aggregator.labels import TERMS, Labels, build_map, clean_label, label_pair, read_lines
from coffee_aggregator.sites import html as dom

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from bs4 import Tag

__all__ = ["Facts", "read_blocks", "read_pairs", "read_text", "vocabulary"]


def vocabulary(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Fold the shared coffee vocabulary, plus one shop's own spellings.

    A bespoke shop is one shop, so it has no platform-wide reading to state;
    what it has is a handful of declensions and private words the two languages
    do not share ("intenzita těla", "lokalita"). Everything else comes from
    :data:`~coffee_aggregator.labels.TERMS`, which is what makes a term the
    vocabulary learns reach every shop at once.

    Args:
        extra: Folded term -> field, applied over the shared vocabulary.

    Returns:
        Folded term -> canonical field.
    """
    return build_map(TERMS, extra=extra)


@dataclass(slots=True)
class Facts:
    """Every labelled value a product page states, read two ways.

    ``labels`` is the shared-vocabulary view every model builder reads, and the
    only one a new shop should need. ``by_label`` keeps the shop's own
    spellings, for the rows whose meaning no canonical field carries — a shop
    that prints both a one-word "Chuť" and a list under "Charakteristika"
    states two different things, and the vocabulary has one flavour field.

    Attributes:
        label_map: Folded label -> field, as :func:`vocabulary` built it.
        labels: The canonical view: ``raw`` for the sink, ``by_field`` for the
            model builders.
        by_label: Folded label -> value, first value wins.
    """

    label_map: dict[str, str] = field(default_factory=dict)
    labels: Labels = field(default_factory=Labels)
    by_label: dict[str, str] = field(default_factory=dict)

    def add(self, label: str | None, value: str | None) -> None:
        """Record one ``label: value`` pair in both views.

        Args:
            label: The label as the shop wrote it.
            value: The value as the shop wrote it.
        """
        cleaned = clean_label(label)
        text = (value or "").strip()
        if not cleaned or not text:
            return
        self.labels.add(cleaned, text, self.label_map)
        self.by_label.setdefault(normalize.fold(cleaned), text)

    def get(self, field_name: str) -> str | None:
        """Return the value mapped onto one canonical field.

        Args:
            field_name: One of the ``F_*`` constants.

        Returns:
            The value, or None when no label fed that field.
        """
        return self.labels.get(field_name)

    def pick(self, *names: str) -> str | None:
        """Return the first of several folded spellings the shop states.

        Args:
            names: Folded labels, in priority order.

        Returns:
            The value, or None when the page states none of them.
        """
        return next((self.by_label[name] for name in names if name in self.by_label), None)

    @property
    def raw(self) -> dict[str, str]:
        """Return the labels as written, upper-cased, for ``raw_attributes``.

        Returns:
            The mapping the sink stores; it is the live one, so a caller may add
            the page metadata to it.
        """
        return self.labels.raw

    def values(self) -> list[str]:
        """Return every value the page states, in page order.

        Returns:
            The values, for the readings that scan all of them at once.
        """
        return list(self.by_label.values())


def read_pairs(pairs: Iterable[tuple[str | None, str | None]], label_map: dict[str, str]) -> Facts:
    """Collect labelled values a shop states as ready-made pairs.

    Args:
        pairs: ``(label, value)`` as the shop wrote them, in page order.
        label_map: Folded label -> field.

    Returns:
        The facts.
    """
    facts = Facts(label_map=label_map)
    for label, value in pairs:
        facts.add(label, value)
    return facts


def read_text(
    lines: Sequence[str],
    label_map: dict[str, str],
    pairs_of: Callable[[str, dict[str, str]], list[tuple[str, str]]] | None = None,
    *,
    bare_labels: bool = True,
) -> tuple[Facts, list[str]]:
    """Read the labelled values out of rendered text lines, prose kept apart.

    Args:
        lines: The rendered lines, in page order.
        label_map: Folded label -> field.
        pairs_of: A shop's own single-line reader, when it states a shape the
            shared one does not.
        bare_labels: Whether a line that is nothing but a label claims the line
            under it as its value. True for a parameter sheet, where that shape
            is four of the five a shop writes. False for a block of marketing
            copy, where a heading is a heading: "100 % Arabika" over a sentence
            names the species and claims the sentence, and both the sentence and
            the headline above it then vanish from the description.

    Returns:
        The facts, and every line that is not a labelled value.
    """
    if bare_labels:
        pairs, prose = read_lines(lines, label_map, pairs_of)
        return read_pairs(pairs, label_map), prose
    read = pairs_of if pairs_of is not None else _one_line
    pairs = []
    prose = []
    for line in lines:
        found = read(line, label_map)
        if found:
            pairs.extend(found)
        else:
            prose.append(line)
    return read_pairs(pairs, label_map), prose


def _one_line(line: str, _label_map: dict[str, str]) -> list[tuple[str, str]]:
    """Read the one ``Label: value`` an ordinary line states.

    Args:
        line: One rendered line.
        _label_map: Unused; the signature is the one ``read_lines`` takes.

    Returns:
        A one-element list, or an empty one when the line is prose.
    """
    return label_pair(line)


def read_blocks(
    blocks: Iterable[Tag | None],
    label_map: dict[str, str],
    *,
    bare_labels: bool = True,
) -> tuple[Facts, list[str]]:
    """Read the labelled values out of description blocks, prose kept apart.

    Line breaks come from the markup rather than from ``Tag.text``, which
    concatenates the strings around every ``<br/>`` with no separator.

    Args:
        blocks: The blocks to read, in priority order; missing ones are skipped.
        label_map: Folded label -> field.
        bare_labels: As :func:`read_text` takes it.

    Returns:
        The facts, and every line that is not a labelled value.
    """
    lines = [line for block in blocks for line in dom.lines(block)]
    return read_text(lines, label_map, bare_labels=bare_labels)
