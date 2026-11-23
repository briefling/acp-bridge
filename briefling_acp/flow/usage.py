"""Usage counters of a task (tokens, cost). The host injects a reader returning cumulative counters."""
from __future__ import annotations

from typing import Callable

USAGE_KEYS = ("in", "out", "cache_read", "cache_write", "reasoning", "cost")

Usage = Callable[[], "dict | None"]


def usage_delta(start: dict | None, end: dict | None) -> dict | None:
    """What a task spent. None when unknown, or when a counter went down (sessions deleted in between)."""
    if not start or not end:
        return None
    d = {k: (end.get(k) or 0) - (start.get(k) or 0) for k in USAGE_KEYS}
    if any(v < 0 for v in d.values()):
        return None
    d["cost"] = round(d["cost"], 4)
    return d
