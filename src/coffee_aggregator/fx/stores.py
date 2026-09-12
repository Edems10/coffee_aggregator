from __future__ import annotations

import json
import logging
import os
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coffee_aggregator.fx.rates import BASE_CURRENCY, QUOTE_CURRENCY, FxRate

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)

TABLE = "fx_rates"
#: Default file name of the no-database store, under the HTTP cache directory.
CACHE_FILE_NAME = "fx_rates.json"

SELECT_ONE_SQL = (
    f"SELECT date, rate, source FROM {TABLE} "  # noqa: S608  (a module constant, not input)
    "WHERE date = %s AND base = %s AND quote = %s"
)
SELECT_LATEST_SQL = (
    f"SELECT date, rate, source FROM {TABLE} "  # noqa: S608  (same)
    "WHERE base = %s AND quote = %s ORDER BY date DESC LIMIT 1"
)
UPSERT_SQL = (
    f"INSERT INTO {TABLE} (date, base, quote, rate, source) "  # noqa: S608  (same)
    "VALUES (%s, %s, %s, %s, %s) "
    "ON CONFLICT (date, base, quote) DO UPDATE SET "
    "rate = EXCLUDED.rate, source = EXCLUDED.source, fetched_at = now()"
)


def default_cache_path(cache_dir: Path | None = None) -> Path:
    """Return where the file store lives when nobody names a path.

    Args:
        cache_dir: The crawler's HTTP cache directory, when one is configured.

    Returns:
        ``<cache dir>/fx_rates.json``, or the same file under
        ``~/.cache/coffee-aggregator`` when no cache directory is set.
    """
    base = cache_dir if cache_dir is not None else Path.home() / ".cache" / "coffee-aggregator"
    return base / CACHE_FILE_NAME


class FileFxStore:
    """A JSON file of fixings, for jsonl runs and machines with no database."""

    def __init__(self, path: Path | str) -> None:
        """Remember where to keep the file; nothing is read or created yet.

        Args:
            path: The JSON file; its parent directories are created on write.
        """
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, str]]:
        """Read the whole file.

        Returns:
            Fixings keyed by ``"<date>|<base>|<quote>"``; empty when the file is
            missing or unreadable — a cache is never load bearing.
        """
        try:
            loaded = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {
            key: value
            for key, value in loaded.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    def get(self, day: date) -> FxRate | None:
        """Return the stored rate of one day.

        Args:
            day: The fixing date wanted.

        Returns:
            The rate, or None when that day is not stored.
        """
        return _from_record(self._load().get(_key(day)))

    def get_latest(self) -> FxRate | None:
        """Return the newest rate in the file.

        Returns:
            The rate with the largest date, or None when the file holds none.
        """
        rates = [rate for rate in map(_from_record, self._load().values()) if rate is not None]
        return max(rates, key=lambda rate: rate.date) if rates else None

    def put(self, rate: FxRate) -> None:
        """Store one rate, replacing whatever was held for that day.

        Args:
            rate: The rate to keep.
        """
        records = self._load()
        records[_key(rate.date, rate.base, rate.quote)] = {
            key: str(value) for key, value in rate.to_record().items()
        }
        payload = json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            temporary.write_text(payload, "utf-8")
            temporary.replace(self.path)
        except OSError:
            logger.warning("could not write the rate cache at %s", self.path)

    def close(self) -> None:
        """Nothing to release; the file is opened only for the length of a call."""


class PostgresFxStore:
    """The ``fx_rates`` table, reached through the same DSN as the sink."""

    def __init__(self, dsn: str) -> None:
        """Remember the DSN; the connection is opened on first use.

        Args:
            dsn: A ``postgresql://`` connection string.
        """
        self.dsn = dsn
        self._connection: psycopg.Connection[Any] | None = None

    @property
    def connection(self) -> psycopg.Connection[Any]:
        """Return the live connection, opening it on first access.

        Returns:
            An open psycopg connection with autocommit disabled.
        """
        import psycopg  # noqa: PLC0415  (only the postgres path pays for the driver)

        if self._connection is None or self._connection.closed:
            self._connection = psycopg.connect(self.dsn, autocommit=False)
        return self._connection

    def get(self, day: date) -> FxRate | None:
        """Return the stored rate of one day.

        Args:
            day: The fixing date wanted.

        Returns:
            The rate, or None when that day is not stored.
        """
        return self._one(SELECT_ONE_SQL, (day, BASE_CURRENCY, QUOTE_CURRENCY))

    def get_latest(self) -> FxRate | None:
        """Return the newest rate in the table.

        Returns:
            The rate with the largest date, or None when the table is empty.
        """
        return self._one(SELECT_LATEST_SQL, (BASE_CURRENCY, QUOTE_CURRENCY))

    def _one(self, sql: str, params: tuple[Any, ...]) -> FxRate | None:
        """Run a single-row query, turning a broken table into a warning.

        Args:
            sql: The statement to run.
            params: Its bound parameters.

        Returns:
            The rate the row describes, or None when there is no row.
        """
        connection = self.connection
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                row = cursor.fetchone()
            connection.commit()
        except Exception:  # a missing table must never abort a crawl
            logger.exception("could not read %s; run init-db", TABLE)
            self._rollback()
            return None
        if row is None:
            return None
        return FxRate(date=row[0], rate=Decimal(str(row[1])), source=str(row[2] or ""))

    def put(self, rate: FxRate) -> None:
        """Insert or refresh one day's fixing.

        Args:
            rate: The rate to store.
        """
        connection = self.connection
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    UPSERT_SQL,
                    (rate.date, rate.base, rate.quote, rate.rate, rate.source),
                )
            connection.commit()
        except Exception:  # a missing table must never abort a crawl
            logger.exception("could not write %s; run init-db", TABLE)
            self._rollback()

    def _rollback(self) -> None:
        """Undo the open transaction, tolerating a connection that is already gone."""
        try:
            self.connection.rollback()
        except Exception:  # a failed rollback must not mask the real error
            logger.exception("rollback failed")

    def close(self) -> None:
        """Commit anything outstanding and close the connection."""
        if self._connection is None or self._connection.closed:
            return
        try:
            self._connection.commit()
        finally:
            self._connection.close()
            self._connection = None


def _key(day: date, base: str = BASE_CURRENCY, quote: str = QUOTE_CURRENCY) -> str:
    return f"{day.isoformat()}|{base}|{quote}"


def _from_record(record: dict[str, str] | None) -> FxRate | None:
    """Rebuild a rate from one JSON object.

    Args:
        record: The stored mapping, or None.

    Returns:
        The rate, or None when the entry is missing or malformed.
    """
    if not record:
        return None
    try:
        day = date.fromisoformat(str(record["date"]))
        rate = Decimal(str(record["rate"]))
    except (KeyError, ValueError, InvalidOperation):
        logger.warning("ignoring an unreadable entry in the rate cache")
        return None
    return FxRate(
        date=day,
        rate=rate,
        source=str(record.get("source", "")),
        base=str(record.get("base", BASE_CURRENCY)),
        quote=str(record.get("quote", QUOTE_CURRENCY)),
    )
