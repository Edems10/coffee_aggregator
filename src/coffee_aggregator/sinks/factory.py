from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from coffee_aggregator.sinks.base import Sink, SinkResult

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["SINKS", "Sink", "SinkResult", "build", "names"]


class SinkFactory(Protocol):
    """How the CLI builds one sink from what the user asked for."""

    def __call__(self, *, out: Path, dsn: str) -> Sink:
        """Build the sink.

        Args:
            out: The ``--out`` path, for file sinks.
            dsn: The resolved database URL, for database sinks.

        Returns:
            The ready-to-use sink.
        """


def _jsonl(*, out: Path, dsn: str) -> Sink:  # noqa: ARG001  (one signature for every sink)
    from coffee_aggregator.sinks.jsonl import JsonlSink  # noqa: PLC0415  (optional path)

    return JsonlSink(out)


def _postgres(*, out: Path, dsn: str) -> Sink:  # noqa: ARG001  (one signature for every sink)
    from coffee_aggregator.sinks.postgres import PostgresSink  # noqa: PLC0415  (optional path)

    return PostgresSink(dsn)


#: Sink name -> factory. The CLI reads its ``--sink`` choices from the keys and
#: builds the chosen one through the value, so adding a sink is one entry here
#: plus one module — the CLI never learns its name.
SINKS: dict[str, SinkFactory] = {"jsonl": _jsonl, "postgres": _postgres}


def names() -> tuple[str, ...]:
    """Return every sink name, for the CLI's ``--sink`` choices.

    Returns:
        The names in declaration order.
    """
    return tuple(SINKS)


def build(name: str, **kwargs: Any) -> Sink:  # noqa: ANN401  (the knobs differ per sink)
    """Build one sink by name.

    Args:
        name: One of :func:`names`.
        **kwargs: ``out`` and ``dsn``; every factory accepts both.

    Returns:
        The sink.

    Raises:
        KeyError: When no sink goes by that name.
    """
    return SINKS[name](**kwargs)
