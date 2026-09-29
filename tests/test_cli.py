from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from coffee_aggregator import cli, config, sinks, sites
from coffee_aggregator.config import ConfigError, Settings
from coffee_aggregator.db import monitoring
from coffee_aggregator.fx import FileFxStore, FxRate, cnb, ecb
from coffee_aggregator.sinks.jsonl import JsonlSink
from coffee_aggregator.sites import loader, registry
from coffee_aggregator.sites.base import ProductRef, SiteAdapter
from conftest import make_coffee
from test_fetcher import MockHTTP, install

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import date

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> MockHTTP:
    """The network, replaced for the duration of one test."""
    return install(monkeypatch)


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


class _Stocked(_Fake):
    """A shop with one product, whose source discovery already holds.

    ``payload`` is what keeps these tests offline: the pipeline parses it
    directly and never asks the fetcher for the detail page.
    """

    site_id = "stocked"
    name = "Stocked Roastery"

    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        yield ProductRef(
            site_id=self.site_id,
            external_id="1",
            url=f"{self.base_url}/detail/1",
            name="Kava",
            payload="<html>kava</html>",
        )


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
    registry.register(_Stocked)
    out = tmp_path / "coffees.jsonl"
    assert cli.main(["crawl", "--site", "stocked", "--out", str(out)]) == cli.EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report[0]["site_id"] == "stocked"
    assert report[0]["written"] == 1
    assert out.exists()


def test_a_shop_that_wrote_nothing_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """The whole point of item 3: a night where every selector rotted is not green."""
    registry.register(_Fake)
    out = tmp_path / "coffees.jsonl"
    with caplog.at_level("ERROR", logger="coffee_aggregator"):
        assert cli.main(["crawl", "--site", "fake", "--out", str(out)]) == cli.EXIT_INCOMPLETE
    report = json.loads(capsys.readouterr().out)
    assert report[0]["discovered"] == 0
    assert "wrote nothing: fake" in caplog.text
    # a run that produced nothing writes nothing: no empty file, no truncation
    assert not out.exists()


def test_a_site_all_summary_names_every_shop_that_wrote_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry.register(_Fake)
    registry.register(_Stocked)
    with caplog.at_level("ERROR", logger="coffee_aggregator"):
        code = cli.main(["crawl", "--site", "all", "--out", str(tmp_path / "o.jsonl")])
    assert code == cli.EXIT_INCOMPLETE
    assert "1 shop(s) wrote nothing: fake" in caplog.text
    assert "stocked" not in caplog.text


def test_crawl_all_with_no_sites_is_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """A daily job with nothing to crawl is a broken deployment, not a quiet night."""
    with caplog.at_level("ERROR", logger="coffee_aggregator"):
        code = cli.main(["crawl", "--site", "all", "--out", str(tmp_path / "o.jsonl")])
    assert code == cli.EXIT_INCOMPLETE
    assert json.loads(capsys.readouterr().out) == []
    assert "no shops were crawled" in caplog.text


def test_a_capped_run_is_partial_but_not_a_failure(tmp_path: Path) -> None:
    """--limit and --max-pages suppress delisting; they do not fail the night."""
    registry.register(_Stocked)
    argv = ["crawl", "--site", "stocked", "--out", str(tmp_path / "o.jsonl")]
    assert cli.main([*argv, "--limit", "1", "--max-pages", "1"]) == cli.EXIT_OK


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
    registry.register(_Stocked)
    monkeypatch.setattr(sites, "load_errors", ["a.toml: boom", "b.toml: boom"])
    with caplog.at_level("WARNING", logger="coffee_aggregator"):
        assert cli.main(["crawl", "--site", "stocked", "--out", str(tmp_path / "o.jsonl")]) == 0
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


@pytest.mark.parametrize("command", ["list-sites", "init-db", "crawl", "parse", "runs"])
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

CNB_ROBOTS = "https://api.cnb.cz/robots.txt"
CNB_JSON = json.dumps(
    {"rates": [{"validFor": "2026-09-11", "amount": 1, "currencyCode": "EUR", "rate": 24.26}]}
)


def test_fx_fetches_and_prints_the_rate(
    http: MockHTTP,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(tmp_path / "fx.json"))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    http.add(CNB_ROBOTS, status=404)
    http.add(cnb.CNB_URL, body=CNB_JSON, content_type="application/json")

    assert cli.main(["fx"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "date": "2026-09-11",
        "base": "EUR",
        "quote": "CZK",
        "rate": "24.26",
        "source": "cnb",
    }
    assert (tmp_path / "fx.json").is_file()


def test_fx_without_refresh_reads_the_stored_rate(
    http: MockHTTP,
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
    assert len(http.calls) == 0


def test_fx_refresh_goes_to_the_bank_even_with_a_stored_rate(
    http: MockHTTP,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache = tmp_path / "fx.json"
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(cache))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    FileFxStore(cache).put(FxRate(date=_today(), rate=Decimal("1.000"), source="stale"))
    http.add(CNB_ROBOTS, status=404)
    http.add(cnb.CNB_URL, body=CNB_JSON, content_type="application/json")

    assert cli.main(["fx", "--refresh"]) == 0

    assert json.loads(capsys.readouterr().out)["rate"] == "24.26"


def test_fx_prints_null_when_nothing_can_be_had(
    http: MockHTTP,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(tmp_path / "fx.json"))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    http.add(CNB_ROBOTS, status=404)
    http.add(cnb.CNB_URL, status=503)
    http.add("https://www.ecb.europa.eu/robots.txt", status=404)
    http.add(ecb.ECB_URL, status=503)

    with caplog.at_level("WARNING", logger="coffee_aggregator"):
        assert cli.main(["fx"]) == 0

    assert json.loads(capsys.readouterr().out) is None
    assert "no EUR/CZK rate available" in caplog.text


def test_a_crawl_stamps_the_days_rate_on_what_it_writes(
    http: MockHTTP,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.register(_Stocked)
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(tmp_path / "fx.json"))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    http.add(CNB_ROBOTS, status=404)
    http.add(cnb.CNB_URL, body=CNB_JSON, content_type="application/json")

    assert cli.main(["crawl", "--site", "stocked", "--out", str(tmp_path / "o.jsonl")]) == 0

    stored = FileFxStore(tmp_path / "fx.json").get_latest()
    assert stored is not None
    assert stored.rate == Decimal("24.26")


def test_a_crawl_proceeds_when_no_rate_can_be_had(
    http: MockHTTP,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry.register(_Stocked)
    monkeypatch.setenv("COFFEE_AGG_FX_CACHE", str(tmp_path / "fx.json"))
    monkeypatch.setenv("COFFEE_AGG_DELAY", "0")
    http.add(CNB_ROBOTS, status=503)
    http.add("https://www.ecb.europa.eu/robots.txt", status=503)

    assert cli.main(["crawl", "--site", "stocked", "--out", str(tmp_path / "o.jsonl")]) == 0
    assert json.loads(capsys.readouterr().out)[0]["site_id"] == "stocked"


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


# --- item 2: the transport knobs reach the fetcher ---------------------------


def _fetcher_for(argv: list[str]) -> PoliteFetcher:
    args = cli.build_parser().parse_args(argv)
    return cli._make_fetcher(Settings.from_env(), args)


def test_the_default_worker_count_is_two() -> None:
    """Four workers on one host measured slightly slower than one."""
    assert config.DEFAULT_WORKERS == 2
    assert Settings.from_env().workers == 2


def test_every_transport_knob_has_a_switch_and_reaches_the_fetcher() -> None:
    fetcher = _fetcher_for(
        [
            "crawl",
            "--site",
            "fake",
            "--timeout",
            "8",
            "--retries",
            "1",
            "--max-retry-wait",
            "10",
            "--robots-retry",
            "30",
            "--workers",
            "3",
            "--site-workers",
            "5",
        ]
    )
    try:
        assert fetcher.timeout_s == 8
        assert fetcher.retries == 1
        assert fetcher.max_retry_wait_s == 10
        assert fetcher.robots._unreachable_retry_s == 30
        assert fetcher.workers == 3
        assert fetcher.hosts_in_flight == 5
    finally:
        fetcher.close()


def test_the_transport_knobs_fall_back_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COFFEE_AGG_TIMEOUT", "7")
    monkeypatch.setenv("COFFEE_AGG_RETRIES", "0")
    monkeypatch.setenv("COFFEE_AGG_MAX_RETRY_WAIT", "5")
    monkeypatch.setenv("COFFEE_AGG_ROBOTS_RETRY", "60")
    monkeypatch.setenv("COFFEE_AGG_BATCH_SIZE", "10")
    settings = Settings.from_env()
    fetcher = cli._make_fetcher(settings, cli.build_parser().parse_args(["crawl", "--site", "x"]))
    try:
        assert (fetcher.timeout_s, fetcher.retries, fetcher.max_retry_wait_s) == (7.0, 0, 5.0)
        assert fetcher.robots._unreachable_retry_s == 60
        assert settings.batch_size == 10
    finally:
        fetcher.close()


def test_the_batch_size_reaches_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.register(_Stocked)
    seen: dict[str, object] = {}

    def fake_run_many(*args: object, **kwargs: object) -> list[object]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(cli, "run_many", fake_run_many)
    argv = ["crawl", "--site", "stocked", "--out", str(tmp_path / "o.jsonl"), "--batch-size", "7"]
    assert cli.main(argv) == cli.EXIT_INCOMPLETE  # no reports at all
    assert seen["batch_size"] == 7


def test_the_deprecated_pool_hosts_spelling_is_no_longer_used() -> None:
    source = Path(cli.__file__).read_text("utf-8")
    assert "pool_hosts" not in source
    assert "hosts_in_flight" in source


# --- item 1: the deadline ----------------------------------------------------


def test_the_deadline_is_a_crawl_switch_and_a_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.build_parser().parse_args(["crawl", "--site", "x", "--deadline", "900"]).deadline
    monkeypatch.setenv("COFFEE_AGG_DEADLINE", "42")
    assert Settings.from_env().deadline_s == 42.0
    monkeypatch.delenv("COFFEE_AGG_DEADLINE")
    assert Settings.from_env().deadline_s is None


def test_a_deadline_that_has_no_time_left_stops_the_crawl(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.register(_Stocked)
    argv = ["crawl", "--site", "stocked", "--out", str(tmp_path / "o.jsonl"), "--deadline", "0"]

    assert cli.main(argv) == cli.EXIT_INCOMPLETE

    report = json.loads(capsys.readouterr().out)[0]
    assert report["deadline_reached"] is True
    assert report["complete"] is False
    assert report["delisted"] == 0


# --- item 5: sharding --------------------------------------------------------


@pytest.mark.parametrize("raw", ["1/4", "4/4", "1/1"])
def test_a_shard_argument_is_parsed(raw: str) -> None:
    assert cli.build_parser().parse_args(["crawl", "--site", "all", "--shard", raw]).shard


@pytest.mark.parametrize("raw", ["0/4", "5/4", "1/0", "nonsense", "1", "1/x"])
def test_a_nonsense_shard_argument_is_rejected(raw: str) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["crawl", "--site", "all", "--shard", raw])


def test_a_shard_crawls_only_its_own_shops(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.register(_Stocked)
    registry.register(_Fake)
    crawled: list[set[str]] = []
    for index in (1, 2):
        argv = [
            "crawl",
            "--site",
            "all",
            "--out",
            str(tmp_path / "o.jsonl"),
            "--shard",
            f"{index}/2",
        ]
        cli.main(argv)
        crawled.append({report["site_id"] for report in json.loads(capsys.readouterr().out)})
    assert crawled[0] | crawled[1] == {"fake", "stocked"}
    assert not crawled[0] & crawled[1]


def test_a_known_crawl_delay_weighs_a_host_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COFFEE_AGG_CRAWL_DELAYS", "slow.sk=30")
    settings = Settings.from_env()
    assert settings.crawl_delay_for("www.slow.sk") == 30.0
    assert settings.crawl_delay_for("fast.sk") == settings.delay_s
    assert settings.crawl_delays["caffeoro.sk"] == 30.0


def test_a_malformed_crawl_delay_list_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COFFEE_AGG_CRAWL_DELAYS", "slow.sk")
    with pytest.raises(ConfigError):
        Settings.from_env()


# --- item 6: init-db --dry-run -----------------------------------------------


def test_init_db_dry_run_prints_the_pending_migrations_and_applies_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied: list[str] = []

    class FakeSink:
        def __init__(self, dsn: str) -> None:
            self.dsn = dsn

        def pending_migrations(self) -> list[str]:
            return ["0001_initial"]

        def init_schema(self) -> list[str]:
            applied.append("ran")
            return []

        def close(self) -> None:
            return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setattr("coffee_aggregator.sinks.postgres.PostgresSink", FakeSink)
    messages: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *args: messages.append(msg % args))

    assert cli.main(["init-db", "--dry-run"]) == 0

    assert messages == ["pending migration: 0001_initial"]
    assert applied == []


def test_init_db_dry_run_says_so_when_nothing_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSink:
        def __init__(self, dsn: str) -> None:
            self.dsn = dsn

        def pending_migrations(self) -> list[str]:
            return []

        def close(self) -> None:
            return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setattr("coffee_aggregator.sinks.postgres.PostgresSink", FakeSink)
    messages: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *args: messages.append(msg % args))

    assert cli.main(["init-db", "--dry-run"]) == 0
    assert messages == ["up to date"]


# --- item 4: the runs command ------------------------------------------------


def test_runs_prints_the_recent_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [{"site": "fake", "written": 0, "started_at": datetime.now(UTC)}]

    class FakeMonitor:
        def __init__(self, dsn: str) -> None:
            self.dsn = dsn
            self.asked: dict[str, object] = {}

        def recent(self, *, site: str | None = None, limit: int = 20) -> list[dict[str, object]]:
            self.asked = {"site": site, "limit": limit}
            built.append(self)
            return rows

        def close(self) -> None:
            return None

    built: list[FakeMonitor] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setattr(monitoring, "PostgresMonitor", FakeMonitor)

    assert cli.main(["runs", "--site", "fake", "--limit", "5"]) == 0

    assert json.loads(capsys.readouterr().out)[0]["site"] == "fake"
    assert built[0].asked == {"site": "fake", "limit": 5}


def test_runs_without_a_dsn_exits_non_zero(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("ERROR", logger="coffee_aggregator"):
        assert cli.main(["runs"]) == cli.EXIT_CONFIG_ERROR
    assert "DATABASE_URL" in caplog.text
