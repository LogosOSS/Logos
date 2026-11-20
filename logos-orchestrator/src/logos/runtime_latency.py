"""Recent observations from cumulative engine latency histograms."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


def _validated_histogram(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict) or "+Inf" not in raw:
        return None
    buckets = []
    try:
        for label, value in raw.items():
            bound, count = float(label), float(value)
            if (not math.isfinite(bound) and label != "+Inf") or bound < 0 or not math.isfinite(count) or count < 0:
                return None
            buckets.append((bound, str(label), count))
    except (ValueError, TypeError, OverflowError):
        return None
    buckets.sort()
    if any(a[2] > b[2] for a, b in zip(buckets, buckets[1:])):
        return None
    return {label: count for _, label, count in buckets}


@dataclass
class HistogramDelta:
    """Yield new samples once per scrape, resetting when engine counters reset.

    The initial snapshot bootstraps learning. Subsequent snapshots contribute
    only new observations, so warmup traffic cannot dominate a changed workload
    indefinitely. Each engine process needs its own instance.
    """

    previous: dict[str, float] = field(default_factory=dict)

    def observe(self, raw: Any) -> dict[str, float] | None:
        current = _validated_histogram(raw)
        if current is None:
            return None
        reset = set(current) != set(self.previous) or current["+Inf"] < self.previous.get("+Inf", 0)
        delta = current if reset else {key: value - self.previous[key] for key, value in current.items()}
        # A partially inconsistent scrape must not corrupt the last good baseline.
        if _validated_histogram(delta) is None:
            return None
        self.previous = current
        return delta if delta["+Inf"] > 0 else None
