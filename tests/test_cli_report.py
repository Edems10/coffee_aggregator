from __future__ import annotations

import json
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


# --- the sub-command ---------------------------------------------------------


@pytest.fixture
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean environment: the report must be asked for a DSN explicitly."""
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def detection(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The detection half and the database, both replaced for one test.

    ``findings`` is replaced on the real module rather than the module being
    replaced in :data:`sys.modules`: ``cmd_report`` resolves it with
    ``from coffee_aggregator.db import report``, which reads the attribute off
    the already-imported package, so swapping the ``sys.modules`` entry is
    ignored the moment anything else has imported the real one.
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

    from coffee_aggregator.db import report  # noqa: PLC0415  (mirrors cmd_report)

    monkeypatch.setattr(report, "findings", fake_findings)
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
    from coffee_aggregator.db import report  # noqa: PLC0415  (mirrors cmd_report)

    def boom(connection: object, *, day: date, history_days: int) -> list[Finding]:
        del connection, day, history_days
        raise RuntimeError

    monkeypatch.setattr(report, "findings", boom)
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
