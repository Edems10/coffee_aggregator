from __future__ import annotations

import json
import re
import sys
import types
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from coffee_aggregator import cli, reporting

DAY = date(2026, 10, 2)


@dataclass(frozen=True, slots=True)
class Finding:
    """The contract of ``coffee_aggregator.db.report.Finding``, built here.

    The detection half lives in another worktree. Rendering reads the five
    fields structurally, so the presentation tests never need that module, a
    live database or a stub committed next to the real one.
    """

    kind: str
    site: str
    summary: str
    detail: dict[str, Any]
    severity: str


def finding(
    kind: str = "parse-gap",
    site: str = "doubleshot",
    summary: str = "doubleshot: price missing on 18 of 54 products",
    detail: dict[str, Any] | None = None,
    severity: str = "low",
) -> Finding:
    return Finding(kind, site, summary, {} if detail is None else detail, severity)


NO_PRODUCTS = Finding(
    kind="no-products",
    site="kava-dnes",
    summary="kava-dnes stored 0 products, 214 in yesterday's run",
    detail={"written": 0, "previous_written": 214, "discovery_ok": False},
    severity="high",
)
PRICE_JUMP = Finding(
    kind="price-jump",
    site="zrnkovakava",
    summary="zrnkovakava: 2 products moved more than 20%",
    detail={
        "products": [
            {
                "name": 'Ethiopia "Guji" <single> & washed',
                "url": "https://zrnkovakava.cz/p/1",
                "previous_price": 349,
                "price": 489,
                "price_per_kg": 1956,
                "previous_price_per_kg": 1396,
                "currency": "CZK",
            },
            {
                "name": "Brazil Santos",
                "previous_price": 300,
                "price": 285,
                "price_per_kg": 1140,
                "currency": "CZK",
            },
        ]
    },
    severity="low",
)
WEIGHT_CHANGE = Finding(
    kind="weight-change",
    site="zrnkovakava",
    summary="zrnkovakava: 1 product changed weight",
    detail={"products": [{"name": "Brazil Santos", "before_g": 250, "after_g": 1000}]},
    severity="low",
)
COVERAGE = Finding(
    kind="coverage-drop",
    site="",
    summary="roast level is stated on 41% of products, 58% a week ago",
    detail={"products": 8123, "previous_products": 8340, "with_roast_pct": 41},
    severity="low",
)
BUSY = [NO_PRODUCTS, PRICE_JUMP, WEIGHT_CHANGE, COVERAGE]


# --- the renderers -----------------------------------------------------------


@pytest.mark.parametrize("fmt", reporting.FORMATS)
def test_every_format_renders_a_clean_day(fmt: str) -> None:
    out = reporting.render([], day=DAY, fmt=fmt)
    assert out.endswith("\n")


def test_a_clean_day_is_one_line_of_text() -> None:
    assert reporting.render([], day=DAY, fmt="text") == "report 2026-10-02: nothing to report\n"


def test_a_clean_day_is_two_lines_of_markdown() -> None:
    out = reporting.render([], day=DAY, history_days=7, fmt="markdown")
    assert out.splitlines() == [
        "# Crawl report — 2026-10-02",
        "",
        "Nothing to report: the crawl of 2026-10-02 looks like the 7 days before it.",
    ]


def test_a_clean_day_is_an_empty_json_array() -> None:
    assert json.loads(reporting.render([], day=DAY, fmt="json")) == []


def test_text_leads_with_the_verdict_and_groups_by_severity() -> None:
    lines = reporting.render(BUSY, day=DAY, fmt="text").splitlines()
    assert lines[0] == "report 2026-10-02: 1 serious, 3 worth a look"
    assert lines[1] == "serious:"
    assert lines[2].startswith("  [no-products] kava-dnes stored 0 products")
    assert "worth a look:" in lines


def test_text_does_not_repeat_a_shop_the_summary_already_names() -> None:
    named = finding(summary="doubleshot: price missing on 18 of 54")
    unnamed = finding(summary="price missing on 18 of 54")
    text = reporting.render([named, unnamed], day=DAY, fmt="text")
    assert "[parse-gap] doubleshot: price missing on 18 of 54" in text
    assert "[parse-gap] doubleshot: price missing on 18 of 54" in text
    assert text.count("doubleshot") == 2


def test_text_caps_the_low_findings_and_says_how_many_more() -> None:
    many = [finding(summary=f"gap {index}") for index in range(reporting.TEXT_LOW_LIMIT + 3)]
    text = reporting.render(many, day=DAY, fmt="text")
    assert "gap 4" in text
    assert "gap 5" not in text
    assert "… and 3 more; see 'report --format markdown'" in text


def test_text_stays_short_on_a_busy_night() -> None:
    assert len(reporting.render(BUSY, day=DAY, fmt="text").splitlines()) <= 10


def test_markdown_carries_the_detail_the_text_leaves_out() -> None:
    out = reporting.render(BUSY, day=DAY, history_days=7, fmt="markdown")
    assert "## Findings per shop" in out
    assert "| `kava-dnes` | 1 | 0 | `no-products` |" in out
    assert "### kava-dnes — `no-products`" in out
    assert "| `previous_written` | `214` |" in out
    assert "```json" in out
    assert "## What these kinds mean" in out
    assert "**`no-products`**" in out


def test_markdown_names_the_comparison_window() -> None:
    assert "the 3 days before it" in reporting.render(BUSY, day=DAY, history_days=3, fmt="markdown")
    assert "the day before it" in reporting.render(BUSY, day=DAY, history_days=1, fmt="markdown")


def test_json_is_the_findings_as_they_arrived() -> None:
    payload = json.loads(reporting.render(BUSY, day=DAY, fmt="json"))
    assert [row["kind"] for row in payload] == [row.kind for row in BUSY]
    assert set(payload[0]) == {"kind", "site", "summary", "detail", "severity"}
    assert payload[0]["detail"]["previous_written"] == 214


def test_an_unknown_format_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown report format"):
        reporting.render([], day=DAY, fmt="pdf")


# --- the page ----------------------------------------------------------------


def _html(found: list[Finding], *, history_days: int = 7) -> str:
    return reporting.render(found, day=DAY, history_days=history_days, fmt="html")


def test_the_page_is_a_whole_document() -> None:
    for out in (_html([]), _html(BUSY)):
        assert out.startswith('<!doctype html>\n<html lang="en">')
        assert out.rstrip().endswith("</html>")
        assert out.count("<main>") == out.count("</main>") == 1
        assert out.count("<table") == out.count("</table>")
        assert out.count("<tr") == out.count("</tr>")


def test_the_page_asks_for_nothing_from_the_network() -> None:
    out = _html(BUSY)
    assert "<link" not in out
    assert "<script" not in out
    assert "<img" not in out
    assert "@import" not in out
    assert "url(" not in out


def test_the_page_themes_itself_in_both_schemes() -> None:
    out = _html([])
    assert ":root {" in out
    assert "@media (prefers-color-scheme: dark)" in out
    assert re.search(r"body \{[^}]*background: var\(--bg\)", out, re.DOTALL)
    assert "width=device-width" in out


def test_a_clean_page_says_so_and_nothing_else() -> None:
    out = _html([])
    assert '<section class="verdict good"><strong>Nothing to report</strong>' in out
    assert "Price movers" not in out
    assert "Findings per shop" not in out


def test_a_busy_page_leads_with_the_count_that_matters() -> None:
    assert '<section class="verdict bad"><strong>1 serious</strong>' in _html(BUSY)
    assert '<section class="verdict warn"><strong>1 worth a look</strong>' in _html([PRICE_JUMP])


def test_the_page_escapes_every_value_it_interpolates() -> None:
    nasty = Finding(
        kind="price-jump",
        site="zrnkovakava",
        summary='a name with <b> & "quotes"',
        detail={"products": [{"name": 'Ethiopia <b> & "Guji"', "previous_price": 1, "price": 2}]},
        severity="low",
    )
    out = _html([nasty])
    assert "Ethiopia &lt;b&gt; &amp; &quot;Guji&quot;" in out
    assert "a name with &lt;b&gt; &amp; &quot;quotes&quot;" in out
    assert "<b>" not in out


def test_the_price_table_sorts_by_the_size_of_the_move() -> None:
    out = _html(BUSY)
    assert "<h2>Price movers</h2>" in out
    guji = out.index("Guji")
    brazil = out.index("Brazil Santos")
    assert guji < brazil  # +40.1% before -5.0%
    assert "+40.1%" in out
    assert "-5.0%" in out


def test_the_price_table_shows_the_comparable_per_kilogram_figure() -> None:
    out = _html(BUSY)
    assert "1396 → 1956 CZK" in out


def test_a_product_that_also_changed_weight_is_marked_as_not_a_price_move() -> None:
    out = _html(BUSY)
    assert '<tr class="flagged">' in out
    assert "weight changed" in out
    assert "are not price moves" in out


def test_a_product_whose_weight_held_is_not_marked() -> None:
    out = _html([PRICE_JUMP])
    assert '<tr class="flagged">' not in out
    assert "are not price moves" not in out


def test_the_page_shows_the_catalogue_totals_and_their_move() -> None:
    out = _html(BUSY)
    assert "<h2>Catalogue</h2>" in out
    assert "8123" in out
    assert "-217 vs 8340" in out


def test_the_page_groups_the_anomalies_with_the_per_shop_numbers() -> None:
    out = _html(BUSY)
    assert "<h2>Findings per shop</h2>" in out
    assert "<h2>Serious</h2>" in out
    assert "<h2>Worth a look</h2>" in out
    assert '<div class="card high">' in out
    assert reporting.KIND_NOTES["no-products"][:40] in out
    assert "catalogue-wide" in out


def test_movers_fall_back_to_a_finding_that_is_one_product() -> None:
    one = Finding(
        kind="price-jump",
        site="fake",
        summary="one product moved",
        detail={"product": "Kenya AA", "previous_price": 100, "price": 150},
        severity="low",
    )
    rows = reporting.movers([one])
    assert [(row.product, row.pct) for row in rows] == [("Kenya AA", 50.0)]


def test_a_price_finding_with_no_prices_makes_no_table() -> None:
    bare = Finding("price-jump", "fake", "something moved", {"note": "unparsed"}, "low")
    assert reporting.movers([bare]) == []
    assert "Price movers" not in _html([bare])


def test_the_page_sets_a_command_in_a_note_as_code() -> None:
    out = _html([NO_PRODUCTS])
    assert "<code>coffee-aggregator runs --site &lt;shop&gt; --limit 5</code>" in out
    assert "`" not in out


def test_the_page_spells_a_boolean_the_way_the_json_does() -> None:
    assert "<td class='n'>false</td>" in _html([NO_PRODUCTS])


def test_a_single_entry_is_counted_as_one_item() -> None:
    assert "<code>products</code> (1 item)" in _html([WEIGHT_CHANGE])
    assert "`products` (1 item):" in reporting.render([WEIGHT_CHANGE], day=DAY, fmt="markdown")


# --- the sub-command ---------------------------------------------------------


@pytest.fixture
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean environment: the report must be asked for a DSN explicitly."""
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def detection(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The detection half and the database, both replaced for one test.

    ``cmd_report`` imports ``coffee_aggregator.db.report`` inside the function,
    so seeding :data:`sys.modules` is enough to stand in for the other half of
    the report without a stub file or a live database.
    """
    asked: dict[str, Any] = {"closed": False}

    class FakeConnection:
        def close(self) -> None:
            asked["closed"] = True

    def fake_findings(connection: object, *, day: date, history_days: int) -> list[Finding]:
        asked["connection"] = connection
        asked["day"] = day
        asked["history_days"] = history_days
        return list(asked.get("found", []))

    def fake_connect(dsn: str) -> FakeConnection:
        asked["dsn"] = dsn
        return FakeConnection()

    module = types.ModuleType("coffee_aggregator.db.report")
    module.findings = fake_findings  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "coffee_aggregator.db.report", module)
    monkeypatch.setattr(cli, "connect", fake_connect)
    return asked


def test_report_asks_the_detection_half_for_the_day_it_was_given(
    detection: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    detection["found"] = BUSY
    assert cli.main(["report", "--day", "2026-10-02", "--dsn", "postgresql://fake/db"]) == 0

    assert detection["dsn"] == "postgresql://fake/db"
    assert detection["day"] == DAY
    assert detection["history_days"] == 7
    assert detection["closed"] is True
    assert capsys.readouterr().out.startswith("report 2026-10-02: 1 serious")


def test_report_defaults_to_today(detection: dict[str, Any]) -> None:
    assert cli.main(["report", "--dsn", "postgresql://fake/db"]) == 0
    assert isinstance(detection["day"], date)


def test_report_passes_the_history_window_through(detection: dict[str, Any]) -> None:
    assert cli.main(["report", "--history-days", "14", "--dsn", "postgresql://x/y"]) == 0
    assert detection["history_days"] == 14


@pytest.mark.parametrize("fmt", reporting.FORMATS)
def test_report_prints_every_format(
    detection: dict[str, Any], capsys: pytest.CaptureFixture[str], fmt: str
) -> None:
    detection["found"] = BUSY
    assert cli.main(["report", "--format", fmt, "--dsn", "postgresql://x/y"]) == 0
    assert capsys.readouterr().out.strip()


def test_report_still_exits_zero_with_serious_findings(
    detection: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    # The nightly run must never be failed twice for the same bad night: the
    # crawl's own exit 1 is the alarm, and this is the explanation.
    detection["found"] = [NO_PRODUCTS]
    assert cli.main(["report", "--dsn", "postgresql://x/y"]) == cli.EXIT_OK
    assert "1 serious" in capsys.readouterr().out


def test_report_closes_the_connection_when_detection_raises(
    monkeypatch: pytest.MonkeyPatch, detection: dict[str, Any]
) -> None:
    module = sys.modules["coffee_aggregator.db.report"]

    def boom(connection: object, *, day: date, history_days: int) -> list[Finding]:
        del connection, day, history_days
        raise RuntimeError

    monkeypatch.setattr(module, "findings", boom)
    with pytest.raises(RuntimeError):
        cli.main(["report", "--dsn", "postgresql://x/y"])
    assert detection["closed"] is True


@pytest.mark.usefixtures("no_database", "detection")
def test_report_without_a_dsn_exits_non_zero(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("ERROR", logger="coffee_aggregator"):
        assert cli.main(["report"]) == cli.EXIT_CONFIG_ERROR
    assert "DATABASE_URL" in caplog.text


def test_report_takes_verbose_like_every_other_subcommand() -> None:
    assert cli.build_parser().parse_args(["report", "-v"]).verbose is True
    assert getattr(cli.build_parser().parse_args(["report"]), "verbose", False) is False


def test_report_defaults_match_the_contract() -> None:
    args = cli.build_parser().parse_args(["report"])
    assert args.day is None
    assert args.history_days == reporting.DEFAULT_HISTORY_DAYS == 7
    assert args.format == "text"


def test_a_day_that_is_not_a_date_is_refused() -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.build_parser().parse_args(["report", "--day", "yesterday"])
    assert exit_info.value.code == 2


def test_an_unknown_format_is_refused_by_the_parser() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["report", "--format", "pdf"])
