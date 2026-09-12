from __future__ import annotations

from coffee_aggregator.sinks.base import Sink, SinkResult
from coffee_aggregator.sinks.factory import SINKS, SinkFactory, build, names

__all__ = ["SINKS", "Sink", "SinkFactory", "SinkResult", "build", "names"]
