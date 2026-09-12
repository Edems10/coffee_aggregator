from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from coffee_aggregator import __version__, sinks, sites
from coffee_aggregator.config import ConfigError, Settings
from coffee_aggregator.http import PoliteFetcher
from coffee_aggregator.pipeline import RunReport, run
from coffee_aggregator.sites.base import ProductRef
from coffee_aggregator.sites.registry import UnknownSiteError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coffee_aggregator.fx import FxRate, FxStore
    from coffee_aggregator.sinks.base import Sink
    from coffee_aggregator.sites.base import SiteAdapter

logger = logging.getLogger("coffee_aggregator")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2


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
    crawl.add_argument("--workers", type=int, help="fetch thread pool size")
    crawl.add_argument("--delay", type=float, help="minimum seconds between requests per host")
    crawl.add_argument("--cache-dir", type=Path, help="directory for the conditional-GET cache")

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


def _make_fetcher(settings: Settings, args: argparse.Namespace) -> PoliteFetcher:
    # Only "crawl" carries the transport switches; "fx" gets the same fetcher
    # with the configured defaults, so the banks are treated like any shop.
    delay = getattr(args, "delay", None)
    workers = getattr(args, "workers", None)
    cache_dir = getattr(args, "cache_dir", None)
    return PoliteFetcher(
        user_agent=settings.user_agent,
        contact=settings.contact,
        delay_s=delay if delay is not None else settings.delay_s,
        workers=workers if workers is not None else settings.workers,
        cache_dir=cache_dir or settings.cache_dir,
        ua_token=settings.ua_token,
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
        applied = sink.init_schema()
    finally:
        sink.close()
    for version in applied:
        logger.info("applied migration: %s", version)
    if not applied:
        logger.info("up to date")
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


def cmd_crawl(settings: Settings, args: argparse.Namespace) -> int:
    """Crawl one shop or every shop.

    Args:
        settings: Environment-derived settings.
        args: Parsed command line arguments.

    Returns:
        The process exit code.
    """
    adapters = _selected_sites(args.site)
    if sites.load_errors:
        logger.warning(
            "%d site(s) could not be loaded; run list-sites for the details",
            len(sites.load_errors),
        )
    sink = _make_sink(settings, args)
    fetcher = _make_fetcher(settings, args)
    dsn = settings.require_database_url(args.dsn) if args.sink != "jsonl" else None
    try:
        fx_rate = _daily_rate(settings, args, fetcher, dsn)
        reports: list[RunReport] = [
            run(
                adapter,
                fetcher,
                sink,
                limit=args.limit,
                max_pages=args.max_pages,
                fx_rate=fx_rate,
            )
            for adapter in adapters
        ]
    finally:
        fetcher.close()
        sink.close()
    _write_json([asdict(report) for report in reports])
    return EXIT_OK


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
    _write_json(coffee.to_record(json_safe=True))
    return EXIT_OK


def _dispatch(settings: Settings, args: argparse.Namespace) -> int:
    """Run the sub-command the user asked for.

    Args:
        settings: Environment-derived settings.
        args: Parsed command line arguments.

    Returns:
        The process exit code.
    """
    if args.command == "list-sites":
        return cmd_list_sites()
    if args.command == "init-db":
        return cmd_init_db(settings, args)
    if args.command == "fx":
        return cmd_fx(settings, args)
    if args.command == "crawl":
        return cmd_crawl(settings, args)
    return cmd_parse(args)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Command line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code: 0 on success, 2 on a configuration error.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(verbose=getattr(args, "verbose", False))
    try:
        return _dispatch(Settings.from_env(), args)
    except (ConfigError, UnknownSiteError) as exc:
        logger.error("configuration error — %s", exc)  # noqa: TRY400  (a traceback helps nobody)
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
