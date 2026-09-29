from __future__ import annotations

import pytest

from coffee_aggregator.robots import RobotsRules

TOKEN = "coffee-aggregator"


def rules(*lines: str) -> RobotsRules:
    """Build rules from the lines of a robots.txt.

    Args:
        lines: The file, one line per argument.

    Returns:
        The parsed rules.
    """
    return RobotsRules("\n".join(lines))


# --- the longest matching rule wins, whatever order the shop wrote them in ----


def test_a_later_disallow_beats_an_opening_allow_all() -> None:
    """Shopify ships exactly this file, and the standard library reads it wrong.

    ``Allow: /`` first, then fifty ``Disallow`` lines. Taking the first match in
    file order lets a crawler into every one of them.
    """
    robots = rules("User-agent: *", "Allow: /", "Disallow: /checkout", "Disallow: /admin")

    assert not robots.can_fetch(TOKEN, "https://shop.sk/checkout")
    assert not robots.can_fetch(TOKEN, "https://shop.sk/admin")
    assert robots.can_fetch(TOKEN, "https://shop.sk/collections/kava")


def test_a_longer_allow_carves_an_exception_out_of_a_disallow() -> None:
    robots = rules("User-agent: *", "Disallow: /api/", "Allow: /api/public/")

    assert not robots.can_fetch(TOKEN, "https://shop.sk/api/secret")
    assert robots.can_fetch(TOKEN, "https://shop.sk/api/public/list")


def test_two_rules_of_equal_length_go_to_allow() -> None:
    robots = rules("User-agent: *", "Disallow: /x/", "Allow: /x/")

    assert robots.can_fetch(TOKEN, "https://shop.sk/x/y")


# --- groups ------------------------------------------------------------------


def test_a_blank_line_does_not_end_a_group() -> None:
    robots = rules("User-agent: *", "", "# a notice the shop put here", "", "Disallow: /kava/")

    assert not robots.can_fetch(TOKEN, "https://shop.sk/kava/brazil")


def test_our_own_token_wins_over_the_wildcard() -> None:
    robots = rules(
        "User-agent: *",
        "Disallow: /",
        f"User-agent: {TOKEN}",
        "Allow: /",
    )

    assert robots.can_fetch(TOKEN, "https://shop.sk/kava/")
    assert not robots.can_fetch("some-other-bot", "https://shop.sk/kava/")


def test_consecutive_user_agent_lines_share_one_set_of_rules() -> None:
    robots = rules("User-agent: Googlebot", f"User-agent: {TOKEN}", "Disallow: /private/")

    assert not robots.can_fetch(TOKEN, "https://shop.sk/private/x")


def test_a_file_with_no_group_for_us_allows_everything() -> None:
    robots = rules("User-agent: Googlebot", "Disallow: /")

    assert robots.can_fetch(TOKEN, "https://shop.sk/anything")


# --- the wildcards the standard defines --------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "blocked"),
    [
        ("/*.json", "/products.json", True),
        ("/*.json", "/products.html", False),
        ("/private$", "/private", True),
        ("/private$", "/private/page", False),
        ("/a/*/b", "/a/x/b", True),
        ("/a/*/b", "/a/x/c", False),
    ],
)
def test_wildcards(pattern: str, path: str, *, blocked: bool) -> None:
    robots = rules("User-agent: *", f"Disallow: {pattern}")

    assert robots.can_fetch(TOKEN, f"https://shop.sk{path}") is not blocked


def test_a_rule_matches_the_query_string_too() -> None:
    robots = rules("User-agent: *", "Disallow: /*?filter=")

    assert not robots.can_fetch(TOKEN, "https://shop.sk/kava/?filter=espresso")
    assert robots.can_fetch(TOKEN, "https://shop.sk/kava/")


def test_an_empty_disallow_forbids_nothing() -> None:
    """``Disallow:`` with nothing after it is the shop opening its doors."""
    robots = rules("User-agent: *", "Disallow:")

    assert robots.can_fetch(TOKEN, "https://shop.sk/kava/")


def test_a_disallow_of_everything_is_honoured() -> None:
    robots = rules("User-agent: *", "Disallow: /")

    assert not robots.can_fetch(TOKEN, "https://shop.sk/kava/")


# --- crawl delay --------------------------------------------------------------


def test_the_crawl_delay_of_our_group_is_read() -> None:
    robots = rules(
        "User-agent: *",
        "Crawl-delay: 20",
        f"User-agent: {TOKEN}",
        "Crawl-delay: 2.5",
    )

    assert robots.crawl_delay(TOKEN) == 2.5
    assert robots.crawl_delay("some-other-bot") == 20


def test_a_decimal_comma_is_still_a_number() -> None:
    robots = rules("User-agent: *", "Crawl-delay: 1,5")

    assert robots.crawl_delay(TOKEN) == 1.5


def test_nonsense_in_a_crawl_delay_is_ignored() -> None:
    robots = rules("User-agent: *", "Crawl-delay: soon")

    assert robots.crawl_delay(TOKEN) is None


def test_an_empty_file_allows_everything() -> None:
    robots = rules("")

    assert robots.can_fetch(TOKEN, "https://shop.sk/kava/")
    assert robots.crawl_delay(TOKEN) is None
