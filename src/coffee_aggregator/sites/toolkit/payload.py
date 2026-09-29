from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from re import Pattern

#: A source that starts with a brace is a JSON document, not a page.
_WHOLE_OBJECT_RE: Final = re.compile(r"^\s*\{")

logger = logging.getLogger(__name__)

__all__ = [
    "as_dict",
    "as_list",
    "as_number",
    "as_str",
    "embedded_json",
    "first_record",
    "json_object",
    "localised",
    "looks_like_json",
    "strings",
]


def as_dict(value: object) -> dict[str, object]:
    """Narrow a decoded JSON value to a mapping.

    Args:
        value: Anything ``json.loads`` produced.

    Returns:
        The mapping, or an empty one when the value is of another shape.
    """
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """Narrow a decoded JSON value to a list.

    Args:
        value: Anything ``json.loads`` produced.

    Returns:
        The list, or an empty one when the value is of another shape.
    """
    return value if isinstance(value, list) else []


def as_str(value: object) -> str | None:
    """Narrow a decoded JSON value to a non-empty string.

    Args:
        value: Anything ``json.loads`` produced.

    Returns:
        The trimmed string, or None when it is absent or empty.
    """
    return value.strip() or None if isinstance(value, str) else None


def as_number(value: object) -> float | None:
    """Narrow a decoded JSON value to a number.

    Args:
        value: Anything ``json.loads`` produced.

    Returns:
        The number, or None when the value is not numeric. Booleans are
        rejected: ``True`` is not a price.
    """
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def localised(value: object, language: str) -> str | None:
    """Read one language out of a ``{"cs": ..., "en": ...}`` field.

    Args:
        value: The localised field, which may also be a plain string.
        language: The language to prefer.

    Returns:
        The text in the wanted language, falling back to any other language.
    """
    if isinstance(value, str):
        return value.strip() or None
    mapping = as_dict(value)
    wanted = as_str(mapping.get(language))
    if wanted is not None:
        return wanted
    return next((text for text in (as_str(item) for item in mapping.values()) if text), None)


def json_object(text: str | None, *, what: str = "payload") -> dict[str, object]:
    """Decode a JSON document into a mapping, refusing to raise on bad input.

    A shop that ships a broken data layer must cost us that one record, not the
    run, so an unreadable document is logged and read as "states nothing".

    Args:
        text: The JSON document.
        what: What was being decoded, for the debug line.

    Returns:
        The decoded mapping, or an empty one.
    """
    if not text:
        return {}
    try:
        return as_dict(json.loads(text))
    except json.JSONDecodeError:
        logger.debug("the %s was not valid JSON", what)
        return {}


def embedded_json(
    source: str,
    pattern: Pattern[str],
    *,
    what: str = "payload",
) -> dict[str, object]:
    """Read the first JSON object a page embeds in its own markup or scripts.

    Args:
        source: The page source, or any text holding the object.
        pattern: A pattern with a ``json`` group around the object.
        what: What was being decoded, for the debug line.

    Returns:
        The decoded mapping, or an empty one when the page embeds none.
    """
    match = pattern.search(source)
    if match is None:
        return {}
    return json_object(match.group("json"), what=what)


def first_record(value: object, key: str) -> dict[str, object]:
    """Return the first mapping of a list a data-layer record hangs under a key.

    Every GA4-style payload states its product as ``{"products": [{...}]}``, and
    the one record we want is the first entry of that list.

    Args:
        value: The decoded data-layer record.
        key: The key whose list holds the records.

    Returns:
        The first mapping, or an empty one.
    """
    entries = as_dict(value).get(key)
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        return entries[0]
    return {}


def looks_like_json(text: str) -> bool:
    """Say whether a source is a bare JSON document rather than a page.

    Args:
        text: A page source, or the payload a reference carries.

    Returns:
        True when the text is a JSON object.
    """
    return _WHOLE_OBJECT_RE.match(text) is not None


def strings(record: Mapping[str, object], keys: Iterable[str]) -> dict[str, str]:
    """Pull named fields out of a data-layer record as upper-cased strings.

    A shop's own codes (EAN, SKU, campaign name) have no typed home and are the
    only stable handle on its catalogue, so they are kept verbatim. Numbers are
    kept too: an EAN arrives as an integer as often as as a string.

    Args:
        record: The decoded record.
        keys: The field names to keep, which become the upper-cased keys.

    Returns:
        Upper-cased key -> value, leaving out every field the record omits.
    """
    collected: dict[str, str] = {}
    for key in keys:
        value = record.get(key)
        text = as_str(value) or (str(value) if isinstance(value, (int, float)) else None)
        if text:
            collected[key.upper()] = text
    return collected
