from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coffee_aggregator.models import Coffee


@dataclass(slots=True, frozen=True)
class SinkResult:
    """How one :meth:`Sink.upsert` call went."""

    written: int = 0
    failed: int = 0

    def __add__(self, other: SinkResult) -> SinkResult:
        """Add two results so a pipeline can accumulate batches.

        Args:
            other: The result of another batch.

        Returns:
            The combined totals.
        """
        return SinkResult(self.written + other.written, self.failed + other.failed)


@runtime_checkable
class Sink(Protocol):
    """Somewhere a crawl can store coffees."""

    def upsert(self, coffees: Sequence[Coffee]) -> SinkResult:
        """Store or update a batch of coffees.

        Args:
            coffees: The batch to write.

        Returns:
            How many rows were written and how many failed.
        """
        ...

    def mark_delisted(self, site_id: str, seen_external_ids: set[str]) -> int:
        """Flag products of one site that this run no longer saw.

        Args:
            site_id: The site whose catalogue was fully walked.
            seen_external_ids: Every id the run did see.

        Returns:
            How many rows were flagged.
        """
        ...

    def close(self) -> None:
        """Flush and release whatever the sink holds open."""
        ...
