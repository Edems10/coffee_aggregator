from __future__ import annotations

import argparse
import json
import logging
import shlex
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from coffee_aggregator import __version__, pipeline, reporting, sinks, sites
from coffee_aggregator.config import ConfigError, Settings
from coffee_aggregator.db import monitoring
from coffee_aggregator.db.connect import connect
from coffee_aggregator.http import PoliteFetcher
from coffee_aggregator.pipeline import Deadline, RunReport, ShardError, run_many
from coffee_aggregator.sinks.records import coffee_record
from coffee_aggregator.sites.base import ProductRef
from coffee_aggregator.sites.registry import UnknownSiteError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from coffee_aggregator.db.monitoring import RunMonitor
    from coffee_aggregator.fx import FxRate, FxStore
    from coffee_aggregator.sinks.base import Sink
    from coffee_aggregator.sites.base import SiteAdapter

logger = logging.getLogger("coffee_aggregator")

EXIT_OK = 0
#: A crawl that ran but did not do its job: see :func:`crawl_exit_code`.
EXIT_INCOMPLETE = 1
EXIT_CONFIG_ERROR = 2


def shard_argument(raw: str) -> tuple[int, int]:
    """Parse a ``--shard i/n`` value.

    Args:
        raw: What the user typed.

    Returns:
        The one-based shard index and the shard count.

    Raises:
        ArgumentTypeError: When it is not two positive integers separated by a
            slash, or names a shard that does not exist.
    """
    index_text, separator, count_text = raw.partition("/")
    if not separator or not index_text.strip().isdigit() or not count_text.strip().isdigit():
        message = f"expected i/n, e.g. 1/4, got {raw!r}"
        raise argparse.ArgumentTypeError(message)
    index, count = int(index_text), int(count_text)
    if count < 1 or not 1 <= index <= count:
        message = f"shard {index} does not exist in a split of {count}"
        raise argparse.ArgumentTypeError(message)
    return index, count


def day_argument(raw: str) -> date:
    """Parse a ``--day`` value.

    Args:
        raw: What the user typed.

    Returns:
        The calendar day it names.

    Raises:
        ArgumentTypeError: When it is not an ISO calendar date.
    """
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        message = f"expected YYYY-MM-DD, got {raw!r}"
        raise argparse.ArgumentTypeError(message) from exc


def build_parser() -> argparse.ArgumentParser:
    """Assemble the argument parser.

    Returns:
        The configured parser.
    """
    # -v works before the sub-command and after it: "crawl --site x -v" is what
    # everybody types, and argparse only allows that if the option is on both.
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS, not False: `parents` shares one action object between the root
    # and every sub-parser, so any real default would let the sub-parser wipe a
    # -v given before the sub-command. Absent therefore means "not asked for".
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="log at DEBUG level",
    )

    parser = argparse.ArgumentParser(
        prog="coffee-aggregator",
        description="Polite multi-site collector of Czech and Slovak coffee bean data.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-sites", help="print every registered shop", parents=[common])

    init_db = subparsers.add_parser(
        "init-db",
        help="apply every pending database migration (idempotent)",
        parents=[common],
    )
    init_db.add_argument("--dsn", help="PostgreSQL DSN; overrides DATABASE_URL")
    init_db.add_argument(
        "--dry-run",
        action="store_true",
        help="print the migrations that would be applied and change nothing",
    )

    runs = subparsers.add_parser(
        "runs",
        help="print what the recent crawls did, newest first",
        parents=[common],
    )
    runs.add_argument("--site", help="only this shop's runs")
    runs.add_argument(
        "--limit",
        type=int,
        default=monitoring.DEFAULT_RECENT_LIMIT,
        help="how many runs to print",
    )
    runs.add_argument("--dsn", help="PostgreSQL DSN; overrides DATABASE_URL")

    report = subparsers.add_parser(
        "report",
        help="print what one day's crawl did that is worth a human's attention",
        parents=[common],
    )
    report.add_argument(
        "--day",
        type=day_argument,
        metavar="YYYY-MM-DD",
        help="the crawl day to report on; defaults to today",
    )
    report.add_argument(
        "--history-days",
        type=int,
        default=reporting.DEFAULT_HISTORY_DAYS,
        help="how many days before it that day is compared against",
    )
    report.add_argument(
        "--format",
        choices=reporting.FORMATS,
        default="text",
        help="text for the journal, markdown to paste into a chat, html for a browser, "
        "json for anything else",
    )
    report.add_argument("--dsn", help="PostgreSQL DSN; overrides DATABASE_URL")

    fx = subparsers.add_parser(
        "fx",
        help="print the stored EUR/CZK rate, fetching it when there is none",
        parents=[common],
    )
    fx.add_argument("--refresh", action="store_true", help="fetch even when a rate is stored")
    fx.add_argument("--dsn", help="PostgreSQL DSN; without one the rate is cached in a file")

    crawl = subparsers.add_parser("crawl", help="crawl one shop or every shop", parents=[common])
    crawl.add_argument("--site", required=True, help="a registered site id, or 'all'")
    crawl.add_argument("--sink", choices=sinks.names(), default="jsonl")
    crawl.add_argument("--out", type=Path, default=Path("coffees.jsonl"), help="jsonl output path")
    crawl.add_argument("--dsn", help="PostgreSQL DSN; overrides DATABASE_URL")
    crawl.add_argument("--limit", type=int, help="stop after N products (partial run)")
    crawl.add_argument("--max-pages", type=int, help="cap listing pages (partial run)")
    crawl.add_argument("--workers", type=int, help="fetch thread pool size, within one shop")
    crawl.add_argument(
        "--site-workers",
        type=int,
        help="how many shops to crawl at the same time (each host keeps its own pace)",
    )
    crawl.add_argument("--delay", type=float, help="minimum seconds between requests per host")
    crawl.add_argument("--cache-dir", type=Path, help="directory for the conditional-GET cache")
    crawl.add_argument(
        "--deadline",
        type=float,
        metavar="SECONDS",
        help="stop the whole crawl after this many seconds (the run is then incomplete, "
        "so nothing is delisted)",
    )
    crawl.add_argument(
        "--shard",
        type=shard_argument,
        metavar="I/N",
        help="crawl only shard I of N, split by host and balanced by estimated cost",
    )
    crawl.add_argument("--batch-size", type=int, help="products fetched and written per round")
    crawl.add_argument("--timeout", type=float, help="per-request timeout in seconds")
    crawl.add_argument("--retries", type=int, help="how often a 429/5xx is retried")
    crawl.add_argument("--max-retry-wait", type=float, help="longest a single retry may wait")
    crawl.add_argument(
        "--robots-retry",
        type=float,
        help="how long an unreadable robots.txt keeps a host closed",
    )

    parse_cmd = subparsers.add_parser(
        "parse", help="parse one saved page and print the JSON", parents=[common]
    )
    parse_cmd.add_argument("--site", required=True, help="a registered site id")
    parse_cmd.add_argument("--file", required=True, type=Path, help="a saved HTML page")
    parse_cmd.add_argument("--url", help="the URL the page came from")
    return parser


def _configure_logging(*, verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _write_json(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


def _selected_sites(site_id: str) -> list[SiteAdapter]:
    if site_id == "all":
        return sites.all_sites()
    return [sites.get(site_id)]


def _chosen[ValueT](args: argparse.Namespace, name: str, fallback: ValueT) -> ValueT:
    """Return a command-line value when it was given, the configured one otherwise.

    Only ``crawl`` carries the transport switches; ``fx`` reaches the same
    builder with none of them, so every one of them is optional here.

    Args:
        args: Parsed command line arguments.
        name: The destination the option parses into.
        fallback: What the environment configured.

    Returns:
        The value to use.
    """
    given = getattr(args, name, None)
    return fallback if given is None else given


def _make_fetcher(settings: Settings, args: argparse.Namespace) -> PoliteFetcher:
    """Build the one fetcher an invocation uses.

    Args:
        settings: Environment-derived settings.
        args: Parsed command line arguments.

    Returns:
        A fetcher carrying every configured transport knob.
    """
    cache_dir = getattr(args, "cache_dir", None)
    return PoliteFetcher(
        user_agent=settings.user_agent,
        contact=settings.contact,
        delay_s=_chosen(args, "delay", settings.delay_s),
        workers=_chosen(args, "workers", settings.workers),
        timeout_s=_chosen(args, "timeout", settings.timeout_s),
        retries=_chosen(args, "retries", settings.retries),
        max_retry_wait_s=_chosen(args, "max_retry_wait", settings.max_retry_wait_s),
        robots_retry_s=_chosen(args, "robots_retry", settings.robots_retry_s),
        cache_dir=cache_dir or settings.cache_dir,
        ua_token=settings.ua_token,
        hosts_in_flight=_chosen(args, "site_workers", settings.site_workers),
    )


def _make_sink(settings: Settings, args: argparse.Namespace) -> Sink:
    dsn = settings.require_database_url(args.dsn) if args.sink != "jsonl" else ""
    return sinks.build(args.sink, out=args.out, dsn=dsn)


def _make_fx_store(settings: Settings, dsn: str | None) -> FxStore:
    """Build the rate store that matches where the run is writing.

    Args:
        settings: Environment-derived settings.
        dsn: The database the run is using, when it uses one.

    Returns:
        The ``fx_rates`` table when there is a database, a JSON file otherwise.
    """
    from coffee_aggregator.fx import FileFxStore, PostgresFxStore  # noqa: PLC0415  (optional path)

    if dsn:
        return PostgresFxStore(dsn)
    path = settings.fx_cache_path()
    logger.debug("caching the EUR/CZK rate in %s", path)
    return FileFxStore(path)


def _close_quietly(store: FxStore) -> None:
    """Release a store that owns a connection; the protocol does not demand one.

    Args:
        store: The store just used.
    """
    closer = getattr(store, "close", None)
    if callable(closer):
        closer()


def _daily_rate(
    settings: Settings,
    args: argparse.Namespace,
    fetcher: PoliteFetcher,
    dsn: str | None,
) -> FxRate | None:
    """Resolve the day's EUR/CZK fixing once for the whole invocation.

    Args:
        settings: Environment-derived settings.
        args: Parsed command line arguments.
        fetcher: The shared polite fetcher; the feeds are paced like any shop.
        dsn: The database the run is using, when it uses one.

    Returns:
        The rate, or None when no feed answered and nothing was stored.
    """
    from coffee_aggregator.fx import FxService  # noqa: PLC0415  (optional path)

    store = _make_fx_store(settings, dsn)
    try:
        rate = FxService(fetcher, store).get_rate(refresh=getattr(args, "refresh", False))
    finally:
        _close_quietly(store)
    if rate is None:
        logger.warning("no EUR/CZK rate available; the normalised price columns stay empty")
    else:
        logger.info(
            "EUR/CZK = %s (fixing of %s, source: %s)",
            rate.rate,
            rate.date,
            rate.source,
        )
    return rate


def cmd_list_sites() -> int:
    """Print every registered shop, one per line.

    Returns:
        The process exit code.
    """
    adapters = sites.all_sites()
    if not adapters:
        logger.info("no sites are registered yet")
    for adapter in adapters:
        logger.info(
            "%s\t%s\t%s\t%s\t%s",
            adapter.site_id,
            adapter.name,
            adapter.country,
            adapter.kind,
            adapter.base_url,
        )
    if sites.load_errors:
        for problem in sites.load_errors:
            logger.error("could not load: %s", problem)
        return EXIT_CONFIG_ERROR
    return EXIT_OK


def cmd_init_db(settings: Settings, args: argparse.Namespace) -> int:
    """Apply the schema to the configured database.

    Args:
        settings: Environment-derived settings.
        args: Parsed command line arguments.

    Returns:
        The process exit code.
    """
    from coffee_aggregator.sinks.postgres import PostgresSink  # noqa: PLC0415  (optional path)

    sink = PostgresSink(settings.require_database_url(args.dsn))
    try:
        if getattr(args, "dry_run", False):
            outstanding = sink.pending_migrations()
            for version in outstanding:
                logger.info("pending migration: %s", version)
            if not outstanding:
                logger.info("up to date")
            return EXIT_OK
        applied = sink.init_schema()
    finally:
        sink.close()
    for version in applied:
        logger.info("applied migration: %s", version)
    if not applied:
        logger.info("up to date")
    return EXIT_OK


def cmd_runs(settings: Settings, args: argparse.Namespace) -> int:
    """Print what the recent crawls did.

    Args:
        settings: Environment-derived settings.
        args: Parsed command line arguments.

    Returns:
        The process exit code.
    """
    monitor = monitoring.PostgresMonitor(settings.require_database_url(args.dsn))
    try:
        rows = monitor.recent(site=args.site, limit=args.limit)
    finally:
        monitor.close()
    if not rows:
        logger.info("no runs recorded yet")
    _write_json(rows)
    return EXIT_OK


def cmd_report(settings: Settings, args: argparse.Namespace) -> int:
    """Print what one day's crawl did that somebody should know about.

    It returns :data:`EXIT_OK` whatever it finds. A night that went badly is
    already an exit 1 from ``crawl``, and a summary that could fail the nightly
    unit a second time would only make the unit cry wolf about data that is
    sitting safely in the database.

    Args:
        settings: Environment-derived settings.
        args: Parsed command line arguments.

    Returns:
        The process exit code.
    """
    from coffee_aggregator.db import report  # noqa: PLC0415  (only the postgres paths pay)

    day = args.day or datetime.now(UTC).date()
    connection = connect(settings.require_database_url(args.dsn))
    try:
        found = report.findings(connection, day=day, history_days=args.history_days)
    finally:
        connection.close()
    sys.stdout.write(
        reporting.render(found, day=day, history_days=args.history_days, fmt=args.format)
    )
    return EXIT_OK


def cmd_fx(settings: Settings, args: argparse.Namespace) -> int:
    """Print the EUR/CZK rate the crawls are stamping on their rows.

    Args:
        settings: Environment-derived settings.
        args: Parsed command line arguments.

    Returns:
        The process exit code.
    """
    dsn = (args.dsn or "").strip() or settings.database_url
    fetcher = _make_fetcher(settings, args)
    try:
        rate = _daily_rate(settings, args, fetcher, dsn)
    finally:
        fetcher.close()
    _write_json(rate.to_record() if rate is not None else None)
    return EXIT_OK


def crawl_exit_code(reports: Sequence[RunReport]) -> int:
    """Decide whether a finished crawl counts as a successful night.

    ``cmd_crawl`` used to return ``EXIT_OK`` whatever happened, so a night where
    every shop's selectors rotted was a green Lambda. The rule is:

    * a shop that stored **no product at all**, or whose **discovery failed**,
      fails the run — those are the two shapes a rotted selector takes;
    * ``--limit``, ``--max-pages`` and ``--deadline`` never fail a run by
      themselves. They make it *partial*, which suppresses delisting; a cap of
      one product is still expected to produce one product;
    * a crawl with nothing to crawl fails too. An empty shop list at three in
      the morning is a broken deployment, not a quiet night.

    Args:
        reports: One report per shop that was crawled.

    Returns:
        :data:`EXIT_OK` or :data:`EXIT_INCOMPLETE`.
    """
    if not reports:
        logger.error("no shops were crawled")
        return EXIT_INCOMPLETE
    broken = [report.site_id for report in reports if not report.discovery_ok]
    empty = pipeline.wrote_nothing(reports)
    if broken:
        logger.error("discovery failed for %d shop(s): %s", len(broken), ", ".join(broken))
    if empty:
        logger.error("%d shop(s) wrote nothing: %s", len(empty), ", ".join(empty))
    if broken or empty:
        return EXIT_INCOMPLETE
    logger.info("every one of the %d shop(s) wrote at least one product", len(reports))
    return EXIT_OK


def _sharded(
    settings: Settings,
    adapters: Sequence[SiteAdapter],
    args: argparse.Namespace,
) -> list[SiteAdapter]:
    """Cut the shop list down to the shard this invocation was given.

    Args:
        settings: Environment-derived settings, for the per-host delays.
        adapters: Every shop the run would otherwise crawl.
        args: Parsed command line arguments.

    Returns:
        The shops of this shard, or all of them when no shard was asked for.
    """
    shard = getattr(args, "shard", None)
    if shard is None:
        return list(adapters)
    index, count = shard
    delay_for = _delay_for(settings, args)
    costs = pipeline.host_costs(adapters, delay_for=delay_for, max_pages=args.max_pages)
    chosen = pipeline.shard_hosts(costs, index, count)
    budget = sum(cost.seconds for cost in chosen)
    logger.info(
        "shard %d/%d: %d host(s), %d shop(s), ~%.0fs of estimated request time",
        index,
        count,
        len(chosen),
        sum(len(cost.site_ids) for cost in chosen),
        budget,
    )
    for cost in chosen[:3]:
        logger.info(
            "  %s: %s (~%d requests at %.1fs, ~%.0fs)",
            cost.host,
            ", ".join(cost.site_ids),
            cost.requests,
            cost.delay_s,
            cost.seconds,
        )
    return pipeline.shard_sites(
        adapters, index, count, delay_for=delay_for, max_pages=args.max_pages
    )


def _delay_for(settings: Settings, args: argparse.Namespace) -> Callable[[str], float]:
    """Return the function that says how slowly one host must be asked.

    Args:
        settings: Environment-derived settings, holding the known crawl-delays.
        args: Parsed command line arguments, whose ``--delay`` wins.

    Returns:
        A callable from host to seconds per request.
    """
    floor = _chosen(args, "delay", settings.delay_s)

    def delay_for(host: str) -> float:
        return max(floor, settings.crawl_delays.get(host, 0.0))

    return delay_for


def cmd_crawl(settings: Settings, args: argparse.Namespace, command: str = "") -> int:
    """Crawl one shop or every shop.

    Args:
        settings: Environment-derived settings.
        args: Parsed command line arguments.
        command: The command line this run was started with, recorded with it.

    Returns:
        The process exit code; see :func:`crawl_exit_code`.
    """
    adapters = _sharded(settings, _selected_sites(args.site), args)
    if sites.load_errors:
        logger.warning(
            "%d site(s) could not be loaded; run list-sites for the details",
            len(sites.load_errors),
        )
    sink = _make_sink(settings, args)
    fetcher = _make_fetcher(settings, args)
    dsn = settings.require_database_url(args.dsn) if args.sink != "jsonl" else None
    deadline_s = _chosen(args, "deadline", settings.deadline_s)
    monitor: RunMonitor = monitoring.build_monitor(dsn, command=command)
    try:
        fx_rate = _daily_rate(settings, args, fetcher, dsn)
        reports: list[RunReport] = run_many(
            adapters,
            fetcher,
            sink,
            site_workers=_chosen(args, "site_workers", settings.site_workers),
            limit=args.limit,
            max_pages=args.max_pages,
            batch_size=_chosen(args, "batch_size", settings.batch_size),
            fx_rate=fx_rate,
            deadline=None if deadline_s is None else Deadline.after(deadline_s),
            monitor=monitor,
        )
    finally:
        fetcher.close()
        sink.close()
        monitor.close()
    _write_json([asdict(report) for report in reports])
    return crawl_exit_code(reports)


def cmd_parse(args: argparse.Namespace) -> int:
    """Parse one saved HTML page and print the resulting coffee as JSON.

    Args:
        args: Parsed command line arguments.

    Returns:
        The process exit code.
    """
    adapter = sites.get(args.site)
    html = Path(args.file).read_text("utf-8")
    url = args.url or f"file://{Path(args.file).resolve()}"
    ref = ProductRef(site_id=adapter.site_id, external_id="", url=url)
    coffee = adapter.parse_product(html, ref)
    if coffee is None:
        logger.warning("%s is not a coffee product", args.file)
        _write_json(None)
        return EXIT_OK
    _write_json(coffee_record(coffee, json_safe=True))
    return EXIT_OK


def _dispatch(settings: Settings, args: argparse.Namespace, command: str) -> int:
    """Run the sub-command the user asked for.

    Args:
        settings: Environment-derived settings.
        args: Parsed command line arguments.
        command: The command line, recorded with every crawl.

    Returns:
        The process exit code.
    """
    handlers: dict[str, Callable[[], int]] = {
        "list-sites": cmd_list_sites,
        "init-db": lambda: cmd_init_db(settings, args),
        "runs": lambda: cmd_runs(settings, args),
        "report": lambda: cmd_report(settings, args),
        "fx": lambda: cmd_fx(settings, args),
        "crawl": lambda: cmd_crawl(settings, args, command),
        "parse": lambda: cmd_parse(args),
    }
    return handlers[args.command]()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Command line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code: 0 on success, 1 when a crawl ran but a shop wrote
        nothing, 2 on a configuration error.
    """
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    _configure_logging(verbose=getattr(args, "verbose", False))
    command = shlex.join(["coffee-aggregator", *arguments])
    try:
        return _dispatch(Settings.from_env(), args, command)
    except (ConfigError, UnknownSiteError, ShardError) as exc:
        logger.error("configuration error — %s", exc)  # noqa: TRY400  (a traceback helps nobody)
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
