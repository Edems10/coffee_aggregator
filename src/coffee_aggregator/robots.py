from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import unquote, urlsplit

#: Lines that open a group. Everything after one, until the next one, belongs to
#: the agents it names.
_AGENT: Final = "user-agent"
_ALLOW: Final = "allow"
_DISALLOW: Final = "disallow"
_CRAWL_DELAY: Final = "crawl-delay"


@dataclass(slots=True, frozen=True)
class Rule:
    """One ``Allow`` or ``Disallow`` line."""

    pattern: str
    allow: bool
    matcher: re.Pattern[str]

    @property
    def specificity(self) -> int:
        """Return how specific the rule is.

        Returns:
            The character length of the path pattern, which is what decides
            between two rules that both match.
        """
        return len(self.pattern)


@dataclass(slots=True)
class Group:
    """The rules of one ``User-agent`` block."""

    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def _compile(pattern: str) -> re.Pattern[str]:
    """Turn a robots path pattern into a regular expression.

    ``*`` stands for any run of characters and a trailing ``$`` anchors the end
    of the path; every other character is literal.

    Args:
        pattern: The path as the shop wrote it.

    Returns:
        A compiled expression that matches from the start of a path.
    """
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    expression = "".join("[^\n]*" if char == "*" else re.escape(char) for char in body)
    return re.compile(f"^{expression}{'$' if anchored else ''}")


def _parse_groups(text: str) -> list[Group]:
    """Split a robots.txt into its groups.

    A group runs until the next ``User-agent`` line, and consecutive
    ``User-agent`` lines share one set of rules. Blank lines and comments end
    nothing, which is where CPython's own parser goes wrong.

    Args:
        text: The body of a robots.txt.

    Returns:
        The groups, in the order the file states them.
    """
    groups: list[Group] = []
    current: Group | None = None
    naming_agents = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        field_name = key.strip().lower()
        value = value.strip()
        if field_name == _AGENT:
            if current is None or not naming_agents:
                current = Group()
                groups.append(current)
                naming_agents = True
            current.agents.append(value.lower())
            continue
        if current is None:
            continue
        naming_agents = False
        if field_name in {_ALLOW, _DISALLOW}:
            rule = _rule(field_name, value)
            if rule is not None:
                current.rules.append(rule)
        elif field_name == _CRAWL_DELAY:
            current.crawl_delay = _delay(value, current.crawl_delay)
    return groups


def _rule(field_name: str, value: str) -> Rule | None:
    """Build one rule from an ``Allow`` or ``Disallow`` line.

    Args:
        field_name: Either ``allow`` or ``disallow``, already lowercased.
        value: The path the shop wrote.

    Returns:
        The rule, or None for a bare ``Disallow:``, which is a shop saying
        nothing is off limits rather than a rule blocking the whole site.
    """
    if field_name == _DISALLOW and not value:
        return None
    path = value if value.startswith("/") else f"/{value}"
    return Rule(pattern=path, allow=field_name == _ALLOW, matcher=_compile(path))


def _delay(value: str, current: float | None) -> float | None:
    """Read a ``Crawl-delay`` line, keeping what we had if it makes no sense.

    Args:
        value: The number the shop wrote, possibly with a decimal comma.
        current: The delay read so far.

    Returns:
        The delay in seconds.
    """
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return current


class RobotsRules:
    """A robots.txt read the way RFC 9309 says to read it.

    Two things in the standard that Python's own :mod:`urllib.robotparser` does
    not do, and both of them quietly hand out permission a shop never gave:

    * A group ends at the next ``User-agent`` line. Blank lines and comments
      inside it mean nothing, while the standard library ends the group at the
      first blank line and drops every rule below it.
    * When several rules match, the longest pattern wins and a tie goes to
      ``Allow``. The standard library returns the first rule in file order, so a
      shop that opens with ``Allow: /`` before fifty ``Disallow`` lines, which is
      exactly what Shopify ships, reads as letting a crawler anywhere.
    """

    def __init__(self, text: str) -> None:
        """Parse a robots.txt.

        Args:
            text: The body of the file.
        """
        self._groups = _parse_groups(text)

    def _group_for(self, ua_token: str) -> Group | None:
        """Pick the group whose rules apply to one crawler.

        Args:
            ua_token: Our product token, matched case-insensitively.

        Returns:
            The group naming us, else the wildcard group, else None.
        """
        token = ua_token.lower()
        wildcard: Group | None = None
        for group in self._groups:
            if token in group.agents:
                return group
            if wildcard is None and "*" in group.agents:
                wildcard = group
        return wildcard

    def can_fetch(self, ua_token: str, url: str) -> bool:
        """Say whether one URL may be requested.

        Args:
            ua_token: Our product token.
            url: The absolute URL a crawl wants.

        Returns:
            True when no rule forbids it.
        """
        group = self._group_for(ua_token)
        if group is None or not group.rules:
            return True
        path = _path_of(url)
        best: Rule | None = None
        for rule in group.rules:
            if not rule.matcher.match(path):
                continue
            if (
                best is None
                or rule.specificity > best.specificity
                # A tie goes to Allow, so a shop can carve an exception out of a
                # broader Disallow.
                or (rule.specificity == best.specificity and rule.allow)
            ):
                best = rule
        return best.allow if best else True

    def crawl_delay(self, ua_token: str) -> float | None:
        """Read the delay the shop asks for.

        Args:
            ua_token: Our product token.

        Returns:
            The delay in seconds, or None when the shop asks for none.
        """
        group = self._group_for(ua_token)
        return group.crawl_delay if group else None


def _path_of(url: str) -> str:
    """Reduce a URL to the part robots rules are written against.

    Args:
        url: An absolute or relative URL.

    Returns:
        The path with its query string, never empty.
    """
    parts = urlsplit(url)
    path = unquote(parts.path) or "/"
    return f"{path}?{parts.query}" if parts.query else path
