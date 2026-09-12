from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
import responses

from coffee_aggregator import cli, sinks, sites
from coffee_aggregator.config import Settings
from coffee_aggregator.fx import FileFxStore, FxRate, cnb, ecb
from coffee_aggregator.sinks.jsonl import JsonlSink
from coffee_aggregator.sites import loader, registry
from coffee_aggregator.sites.base import ProductRef, SiteAdapter
from conftest import make_coffee

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import date
    from pathlib import Path

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee


def _today() -> date:
    return datetime.now(UTC).date()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No DATABASE_URL, no .env in sight, and a registry the test owns."""
    monkeypatch.chdir(tmp_path)
    for name in (
        "DATABASE_URL",
        "COFFEE_AGG_CACHE_DIR",
        "COFFEE_AGG_DELAY",
        "COFFEE_AGG_USER_AGENT",
        "COFFEE_AGG_UA_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    # Every CLI command resolves the day's rate. Point the store somewhere the
    # test owns and seed it, so a command that is not about FX never leaves the
    # machine; the FX tests below override the path with an empty file.
    seeded = tmp_path / "seeded_fx.json"
    FileFxStore(seeded).put(FxRate(date=_today(), rate=Decimal("25.000"), source="test"))
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(seeded))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    saved_classes = dict(registry._REGISTRY)
    saved_instances = dict(registry._INSTANCES)
    registry.clear()
    monkeypatch.setattr(loader, "_loaded", True)
    yield
    registry.clear()
    registry._REGISTRY.update(saved_classes)
    registry._INSTANCES.update(saved_instances)


class _Fake(SiteAdapter):
    site_id = "fake"
    name = "Fake Roastery"
    country = "SK"
    base_url = "https://fake.example.sk"

    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        return iter(())

    def parse_product(self, html: str, ref: ProductRef) -> Coffee | None:
        if "cascara" in html:
            return None
        return make_coffee(site=self.site_id, external_id="1")


def test_list_sites_works_with_zero_registered_sites(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="coffee_aggregator"):
        assert cli.main(["list-sites"]) == 0
    assert "no sites are registered yet" in caplog.text


def test_list_sites_prints_a_registered_site(caplog: pytest.LogCaptureFixture) -> None:
    registry.register(_Fake)
    with caplog.at_level("INFO", logger="coffee_aggregator"):
        assert cli.main(["list-sites"]) == 0
    assert "fake" in caplog.text


def test_init_db_without_a_dsn_exits_non_zero_with_an_actionable_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("ERROR", logger="coffee_aggregator"):
        assert cli.main(["init-db"]) == cli.EXIT_CONFIG_ERROR
    assert "DATABASE_URL" in caplog.text
    assert "postgresql://coffee:coffee@localhost:5432/coffee" in caplog.text
    assert "docker-compose.yml" in caplog.text


def test_crawl_with_the_postgres_sink_and_no_dsn_exits_non_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry.register(_Fake)
    with caplog.at_level("ERROR", logger="coffee_aggregator"):
        assert cli.main(["crawl", "--site", "fake", "--sink", "postgres"]) == cli.EXIT_CONFIG_ERROR
    assert "DATABASE_URL" in caplog.text


def test_crawl_of_an_unknown_site_exits_non_zero(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("ERROR", logger="coffee_aggregator"):
        assert cli.main(["crawl", "--site", "nope"]) == cli.EXIT_CONFIG_ERROR
    assert "unknown site" in caplog.text


def test_crawl_writes_a_jsonl_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    registry.register(_Fake)
    out = tmp_path / "coffees.jsonl"
    assert cli.main(["crawl", "--site", "fake", "--out", str(out)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report[0]["site_id"] == "fake"
    assert report[0]["discovered"] == 0
    # a run that produced nothing writes nothing: no empty file, no truncation
    assert not out.exists()


def test_crawl_all_with_no_sites_is_a_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["crawl", "--site", "all", "--out", str(tmp_path / "o.jsonl")]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_parse_prints_the_coffee_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.register(_Fake)
    page = tmp_path / "page.html"
    page.write_text("<html>kava</html>", encoding="utf-8")
    assert cli.main(["parse", "--site", "fake", "--file", str(page)]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["site"] == "fake"
    assert record["name"]


def test_parse_prints_null_for_a_non_coffee_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.register(_Fake)
    page = tmp_path / "page.html"
    page.write_text("<html>cascara</html>", encoding="utf-8")
    assert cli.main(["parse", "--site", "fake", "--file", str(page)]) == 0
    assert json.loads(capsys.readouterr().out) is None


def test_robots_enforcement_has_no_cli_switch() -> None:
    help_text = cli.build_parser().format_help()
    assert "robots" not in help_text.lower()


def test_unknown_subcommand_is_rejected() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["nonsense"])
    assert excinfo.value.code != 0


def test_a_browser_shaped_user_agent_is_warned_about(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """robots.txt is matched on the FIRST token, so this one claims Firefox's rules."""
    monkeypatch.setenv(
        "COFFEE_AGG_USER_AGENT",
        "Mozilla/5.0 (compatible; coffee-aggregator/0.1)",
    )
    with caplog.at_level("WARNING", logger="coffee_aggregator.config"):
        settings = Settings.from_env()
    assert "browser name" in caplog.text
    assert settings.ua_token is None


def test_a_user_agent_hiding_our_name_later_is_warned_about(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("COFFEE_AGG_USER_AGENT", "beanbot/2.0 coffee-aggregator")
    with caplog.at_level("WARNING", logger="coffee_aggregator.config"):
        Settings.from_env()
    assert "only after the first token" in caplog.text


def test_a_plain_user_agent_is_not_warned_about(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("COFFEE_AGG_USER_AGENT", "coffee-aggregator/0.1 (+https://x/)")
    with caplog.at_level("WARNING", logger="coffee_aggregator.config"):
        settings = Settings.from_env()
    assert caplog.text == ""
    assert settings.user_agent.startswith("coffee-aggregator/")


def test_the_ua_token_override_reaches_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COFFEE_AGG_UA_TOKEN", "grinder-bot")
    assert Settings.from_env().ua_token == "grinder-bot"


# --- item 26: discovery failures are surfaced, not swallowed -----------------


def test_list_sites_exits_non_zero_when_a_config_could_not_be_loaded(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sites, "load_errors", ["shop.toml: TOMLDecodeError: boom"])
    with caplog.at_level("ERROR", logger="coffee_aggregator"):
        assert cli.main(["list-sites"]) == cli.EXIT_CONFIG_ERROR
    assert "shop.toml" in caplog.text


def test_a_crawl_logs_how_many_sites_could_not_be_loaded(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.register(_Fake)
    monkeypatch.setattr(sites, "load_errors", ["a.toml: boom", "b.toml: boom"])
    with caplog.at_level("WARNING", logger="coffee_aggregator"):
        assert cli.main(["crawl", "--site", "fake", "--out", str(tmp_path / "o.jsonl")]) == 0
    assert "2 site(s) could not be loaded" in caplog.text


# --- item 27: the CLI never names a sink class -------------------------------


def test_the_sink_choices_come_from_the_sink_registry() -> None:
    for name in sinks.names():
        parsed = cli.build_parser().parse_args(["crawl", "--site", "x", "--sink", name])
        assert parsed.sink == name
    assert set(sinks.names()) == {"jsonl", "postgres"}
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["crawl", "--site", "x", "--sink", "sqlite"])


def test_the_sink_registry_builds_each_sink(tmp_path: Path) -> None:
    jsonl = sinks.build("jsonl", out=tmp_path / "o.jsonl", dsn="")
    assert isinstance(jsonl, JsonlSink)
    jsonl.close()
    postgres = sinks.build("postgres", out=tmp_path / "o.jsonl", dsn="postgresql://fake/db")
    assert postgres.__class__.__name__ == "PostgresSink"


# --- item 28: -v works before and after the sub-command ----------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["-v", "crawl", "--site", "fake"],
        ["crawl", "--site", "fake", "-v"],
        ["crawl", "-v", "--site", "fake"],
        ["crawl", "--site", "fake", "--verbose"],
    ],
)
def test_verbose_is_accepted_on_the_subcommand_too(argv: list[str]) -> None:
    assert cli.build_parser().parse_args(argv).verbose is True


@pytest.mark.parametrize("command", ["list-sites", "init-db", "crawl", "parse"])
def test_every_subcommand_takes_verbose(command: str) -> None:
    required = {
        "crawl": ["--site", "fake"],
        "parse": ["--site", "fake", "--file", "p.html"],
    }
    argv = [command, *required.get(command, []), "-v"]
    assert cli.build_parser().parse_args(argv).verbose is True
    plain = cli.build_parser().parse_args([command, *required.get(command, [])])
    assert getattr(plain, "verbose", False) is False


# --- the fx sub-command and the once-a-day rate ------------------------------

CNB_ROBOTS = "https://www.cnb.cz/robots.txt"
CNB_TEXT = "11.09.2026 #176\nzemě|měna|množství|kód|kurz\nEMU|euro|1|EUR|24,260\n"


@responses.activate
def test_fx_fetches_and_prints_the_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(tmp_path / "fx.json"))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    responses.add(responses.GET, CNB_ROBOTS, status=404)
    responses.add(responses.GET, cnb.CNB_URL, body=CNB_TEXT, content_type="text/plain")

    assert cli.main(["fx"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "date": "2026-09-11",
        "base": "EUR",
        "quote": "CZK",
        "rate": "24.260",
        "source": "cnb",
    }
    assert (tmp_path / "fx.json").is_file()


@responses.activate
def test_fx_without_refresh_reads_the_stored_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache = tmp_path / "fx.json"
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(cache))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    FileFxStore(cache).put(FxRate(date=_today(), rate=Decimal("24.500"), source="cnb"))

    assert cli.main(["fx"]) == 0

    assert json.loads(capsys.readouterr().out)["rate"] == "24.500"
    assert len(responses.calls) == 0


@responses.activate
def test_fx_refresh_goes_to_the_bank_even_with_a_stored_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache = tmp_path / "fx.json"
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(cache))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    FileFxStore(cache).put(FxRate(date=_today(), rate=Decimal("1.000"), source="stale"))
    responses.add(responses.GET, CNB_ROBOTS, status=404)
    responses.add(responses.GET, cnb.CNB_URL, body=CNB_TEXT, content_type="text/plain")

    assert cli.main(["fx", "--refresh"]) == 0

    assert json.loads(capsys.readouterr().out)["rate"] == "24.260"


@responses.activate
def test_fx_prints_null_when_nothing_can_be_had(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(tmp_path / "fx.json"))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    responses.add(responses.GET, CNB_ROBOTS, status=404)
    responses.add(responses.GET, cnb.CNB_URL, status=503)
    responses.add(responses.GET, "https://www.ecb.europa.eu/robots.txt", status=404)
    responses.add(responses.GET, ecb.ECB_URL, status=503)

    with caplog.at_level("WARNING", logger="coffee_aggregator"):
        assert cli.main(["fx"]) == 0

    assert json.loads(capsys.readouterr().out) is None
    assert "no EUR/CZK rate available" in caplog.text


@responses.activate
def test_a_crawl_stamps_the_days_rate_on_what_it_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.register(_Fake)
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(tmp_path / "fx.json"))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    responses.add(responses.GET, CNB_ROBOTS, status=404)
    responses.add(responses.GET, cnb.CNB_URL, body=CNB_TEXT, content_type="text/plain")

    assert cli.main(["crawl", "--site", "fake", "--out", str(tmp_path / "o.jsonl")]) == 0

    stored = FileFxStore(tmp_path / "fx.json").get_latest()
    assert stored is not None
    assert stored.rate == Decimal("24.260")


@responses.activate
def test_a_crawl_proceeds_when_no_rate_can_be_had(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry.register(_Fake)
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(tmp_path / "fx.json"))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    responses.add(responses.GET, CNB_ROBOTS, status=503)
    responses.add(responses.GET, "https://www.ecb.europa.eu/robots.txt", status=503)

    assert cli.main(["crawl", "--site", "fake", "--out", str(tmp_path / "o.jsonl")]) == 0
    assert json.loads(capsys.readouterr().out)[0]["site_id"] == "fake"


def test_init_db_reports_the_versions_it_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[list[str]] = [["0001_initial", "0002_fx_rates"], []]

    class FakeSink:
        def __init__(self, dsn: str) -> None:
            self.dsn = dsn

        def init_schema(self) -> list[str]:
            return applied.pop(0)

        def close(self) -> None:
            return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setattr("coffee_aggregator.sinks.postgres.PostgresSink", FakeSink)
    messages: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *args: messages.append(msg % args))

    assert cli.main(["init-db"]) == 0
    assert messages == ["applied migration: 0001_initial", "applied migration: 0002_fx_rates"]

    messages.clear()
    assert cli.main(["init-db"]) == 0
    assert messages == ["up to date"]


def test_every_subcommand_including_fx_takes_verbose() -> None:
    assert cli.build_parser().parse_args(["fx", "-v"]).verbose is True
    assert cli.build_parser().parse_args(["fx", "--refresh"]).refresh is True
