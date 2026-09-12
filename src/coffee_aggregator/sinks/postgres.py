from __future__ import annotations

import logging
from itertools import batched
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.types.json import Jsonb

from coffee_aggregator.db import migrate
from coffee_aggregator.sinks.base import SinkResult

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from coffee_aggregator.models import Coffee

logger = logging.getLogger(__name__)


#: Every data column of ``coffee``, in the order ``Coffee.to_record()`` emits
#: them. The INSERT statement, the schema test and the record keys all derive
#: from this one tuple so they cannot drift apart.
COLUMNS: tuple[str, ...] = (
    "site",
    "external_id",
    "url",
    "name",
    "site_country",
    "price",
    "currency",
    "weight_g",
    "price_per_kg",
    "price_eur",
    "price_czk",
    "price_per_kg_eur",
    "price_per_kg_czk",
    "fx_rate_eur_czk",
    "fx_date",
    "available",
    "decaf",
    "origin_country",
    "origin_region",
    "origin_farm",
    "origin_producer",
    "origin_washing_station",
    "altitude_min_m",
    "altitude_max_m",
    "altitude_raw",
    "variety",
    "harvest",
    "process_method",
    "process_methods",
    "process_raw",
    "roast_level",
    "roast_raw",
    "roast_profile",
    "roast_date",
    "best_before",
    "arabica_pct",
    "robusta_pct",
    "is_blend",
    "body",
    "bitterness",
    "acidity",
    "sweetness",
    "taste_scale_max",
    "flavor_notes",
    "tasting_text",
    "brewing_methods",
    "sca_score",
    "rating",
    "rating_max",
    "review_count",
    "reviews",
    "sold_count",
    "variants",
    "images",
    "tags",
    "categories",
    "certifications",
    "awards",
    "specialty_grade",
    "original_price",
    "description",
    "origin_text",
    "raw_attributes",
    "scraped_at",
)

#: Columns stored as jsonb; their values are wrapped in :class:`Jsonb`.
JSON_COLUMNS: frozenset[str] = frozenset(
    {
        "variety",
        "flavor_notes",
        "brewing_methods",
        "process_methods",
        "reviews",
        "variants",
        "images",
        "tags",
        "categories",
        "certifications",
        "awards",
        "raw_attributes",
    }
)

#: Columns the database owns and the sink never overwrites from a record.
MANAGED_COLUMNS: tuple[str, ...] = ("first_seen_at", "last_seen_at", "delisted_at")

_KEY_COLUMNS = ("site", "external_id")
_UPDATABLE = tuple(column for column in COLUMNS if column not in _KEY_COLUMNS)

UPSERT_SQL = (
    f"INSERT INTO coffee ({', '.join(COLUMNS)}) "  # noqa: S608  (column names are a module constant)
    f"VALUES ({', '.join(['%s'] * len(COLUMNS))}) "
    "ON CONFLICT (site, external_id) DO UPDATE SET "
    + ", ".join(f"{column} = EXCLUDED.{column}" for column in _UPDATABLE)
    + ", last_seen_at = now(), delisted_at = NULL"
)

#: The data columns of ``price_history``, after the ``id``/``seen_at`` pair the
#: database fills in itself. One tuple again, so the INSERT and the schema test
#: read from the same place.
PRICE_HISTORY_COLUMNS: tuple[str, ...] = (
    "site",
    "external_id",
    "price",
    "currency",
    "weight_g",
    "available",
    "price_eur",
    "price_czk",
    "fx_rate_eur_czk",
)

PRICE_HISTORY_SQL = (
    f"INSERT INTO price_history (site, external_id, seen_at, "  # noqa: S608  (a module constant)
    f"{', '.join(PRICE_HISTORY_COLUMNS[2:])}) "
    f"VALUES (%s, %s, now(), {', '.join(['%s'] * (len(PRICE_HISTORY_COLUMNS) - 2))})"
)

DELIST_SQL = (
    "UPDATE coffee SET delisted_at = now() "
    "WHERE site = %s AND delisted_at IS NULL AND NOT (external_id = ANY(%s))"
)


def row_for(coffee: Coffee) -> tuple[Any, ...]:
    """Turn one coffee into the positional parameters of :data:`UPSERT_SQL`.

    Args:
        coffee: The coffee to store.

    Returns:
        One value per entry of :data:`COLUMNS`, jsonb values already wrapped.
    """
    record = coffee.to_record(json_safe=False)
    return tuple(
        Jsonb(record[column]) if column in JSON_COLUMNS else record[column] for column in COLUMNS
    )


def price_row_for(coffee: Coffee) -> tuple[Any, ...]:
    """Turn one coffee into the parameters of :data:`PRICE_HISTORY_SQL`.

    Args:
        coffee: The coffee whose price is being recorded.

    Returns:
        The positional parameters.
    """
    return (
        coffee.site,
        coffee.external_id,
        coffee.price,
        coffee.currency,
        coffee.weight_g,
        coffee.available,
        coffee.price_eur,
        coffee.price_czk,
        coffee.fx_rate_eur_czk,
    )


def schema_columns(table: str = "coffee") -> list[str]:
    """Return the columns the packaged migrations leave a table with.

    Args:
        table: The table to collect, ``"coffee"`` or ``"price_history"``.

    Returns:
        The column names in the order the migrations introduce them.
    """
    return migrate.table_columns(table)


class PostgresSink:
    """Upserts coffees and appends a price-history row, one transaction per batch."""

    def __init__(self, dsn: str, *, chunk_size: int = 200) -> None:
        """Remember the DSN; the connection is opened on first use.

        Args:
            dsn: A ``postgresql://`` connection string.
            chunk_size: How many rows go into one ``executemany`` call.
        """
        self.dsn = dsn
        self.chunk_size = max(1, chunk_size)
        self._connection: psycopg.Connection[Any] | None = None

    @property
    def connection(self) -> psycopg.Connection[Any]:
        """Return the live connection, opening it on first access.

        Returns:
            An open psycopg connection with autocommit disabled.
        """
        if self._connection is None or self._connection.closed:
            logger.debug("connecting to postgres")
            self._connection = psycopg.connect(self.dsn, autocommit=False)
        return self._connection

    def init_schema(self) -> list[str]:
        """Apply every pending migration; safe to re-run.

        Returns:
            The versions this call applied, empty when the database was already
            up to date.
        """
        return migrate.apply_migrations(self.connection)

    def pending_migrations(self) -> list[str]:
        """Return the migrations this database has not seen yet.

        Returns:
            The pending versions, in the order they would be applied.
        """
        return migrate.pending(self.connection)

    def upsert(self, coffees: Sequence[Coffee]) -> SinkResult:
        """Write a batch of coffees and their price-history rows.

        Args:
            coffees: The batch to write.

        Returns:
            How many rows were written; a failed batch reports them as failed.
        """
        if not coffees:
            return SinkResult()
        connection = self.connection
        try:
            with connection.cursor() as cursor:
                for chunk in self._chunks(coffees):
                    cursor.executemany(UPSERT_SQL, [row_for(coffee) for coffee in chunk])
                    cursor.executemany(
                        PRICE_HISTORY_SQL, [price_row_for(coffee) for coffee in chunk]
                    )
            connection.commit()
        except Exception:
            # Not only psycopg.Error: building a row can raise too (a date the
            # adapter mis-parsed, a value that will not serialise), and leaving
            # that transaction open would poison every later batch of the run.
            logger.exception("upsert of %d coffees failed; rolling back", len(coffees))
            self._rollback()
            return SinkResult(written=0, failed=len(coffees))
        return SinkResult(written=len(coffees), failed=0)

    def _rollback(self) -> None:
        """Undo the open transaction, tolerating a connection that is already gone."""
        try:
            self.connection.rollback()
        except Exception:  # a failed rollback must not mask the real error
            logger.exception("rollback failed")

    def _chunks(self, coffees: Sequence[Coffee]) -> Iterator[tuple[Coffee, ...]]:
        yield from batched(coffees, self.chunk_size, strict=False)

    def mark_delisted(self, site_id: str, seen_external_ids: set[str]) -> int:
        """Stamp ``delisted_at`` on every product of a site the run did not see.

        Args:
            site_id: The site whose catalogue was fully walked.
            seen_external_ids: Every id the run did see.

        Returns:
            How many rows were stamped.
        """
        if not seen_external_ids:
            # An empty set means "the run saw nothing", which is a broken crawl,
            # not an empty catalogue; delisting on it would wipe the whole shop.
            logger.warning("%s: refusing to delist, the run saw no products at all", site_id)
            return 0
        connection = self.connection
        try:
            with connection.cursor() as cursor:
                cursor.execute(DELIST_SQL, (site_id, list(seen_external_ids)))
                count = cursor.rowcount
            connection.commit()
        except Exception:  # one shop's failure never aborts a --site all run
            logger.exception("delisting for %s failed", site_id)
            self._rollback()
            return 0
        return max(0, count)

    def close(self) -> None:
        """Commit anything outstanding and close the connection."""
        if self._connection is None or self._connection.closed:
            return
        try:
            self._connection.commit()
        finally:
            self._connection.close()
            self._connection = None
