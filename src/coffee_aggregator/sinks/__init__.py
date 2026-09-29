from __future__ import annotations

from coffee_aggregator.sinks.base import Sink, SinkResult
from coffee_aggregator.sinks.factory import SINKS, SinkFactory, build, names
from coffee_aggregator.sinks.records import coffee_record, review_record, variant_record

__all__ = [
    "SINKS",
    "Sink",
    "SinkFactory",
    "SinkResult",
    "build",
    "coffee_record",
    "names",
    "review_record",
    "variant_record",
]
