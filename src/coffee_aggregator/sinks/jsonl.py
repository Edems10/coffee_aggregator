from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from coffee_aggregator.sinks.base import SinkResult

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

    from coffee_aggregator.models import Coffee

logger = logging.getLogger(__name__)


class JsonlSink:
    """Write one JSON object per coffee to a UTF-8 file, atomically.

    The rows go to a sibling ``.tmp`` file that replaces the real output only on
    :meth:`close`, so a crawl that dies halfway — or one that a shop's outage
    leaves with two products — never truncates yesterday's complete file. The
    temporary file is not opened until the first row arrives, so a run that
    writes nothing leaves the previous output alone.
    """

    def __init__(self, path: Path | str) -> None:
        """Remember where to write; nothing is opened yet.

        Args:
            path: Where to write; parent directories are created on first write.
        """
        self.path = Path(path)
        self.temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        self._handle: TextIO | None = None

    def _open(self) -> TextIO:
        """Open the temporary file on first use.

        Returns:
            The open handle.
        """
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.temporary.open("w", encoding="utf-8")
        return self._handle

    def upsert(self, coffees: Sequence[Coffee]) -> SinkResult:
        """Write a batch of coffees as JSON lines.

        Args:
            coffees: The batch to write.

        Returns:
            How many lines were written and how many could not be serialised.
        """
        if not coffees:
            return SinkResult()
        handle = self._open()
        written = 0
        failed = 0
        for coffee in coffees:
            try:
                line = json.dumps(coffee.to_record(json_safe=True), ensure_ascii=False)
            except (TypeError, ValueError):
                logger.exception("could not serialise %s", coffee.url)
                failed += 1
                continue
            handle.write(f"{line}\n")
            written += 1
        handle.flush()
        return SinkResult(written=written, failed=failed)

    def mark_delisted(self, site_id: str, seen_external_ids: set[str]) -> int:
        """Do nothing: a flat file has no history to reconcile against.

        Args:
            site_id: The site that was fully walked.
            seen_external_ids: Every id the run saw.

        Returns:
            Always zero.
        """
        logger.info(
            "jsonl sink cannot delist; %s saw %d products",
            site_id,
            len(seen_external_ids),
        )
        return 0

    def close(self) -> None:
        """Put the finished file in place; a run that wrote nothing changes nothing."""
        if self._handle is None:
            return
        self._handle.close()
        self._handle = None
        self.temporary.replace(self.path)
