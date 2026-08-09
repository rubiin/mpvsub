"""Caching for search results.

Keeps recently returned subtitle lists in memory for a short TTL so that
re-searching (e.g. switching language filters or sorting) stays instant and
repeated network calls are avoided.  The OpenSubtitles client keys the
cache by query + sort mode, see :mod:`opensubtitles_client`.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from models import SubtitleResult

log = logging.getLogger(__name__)


class SearchCache:
    """Simple TTL key → list cache, safe to use from any thread."""

    def __init__(self, ttl: float = 600.0) -> None:
        self._ttl = ttl
        self._items: dict[str, tuple[float, list[SubtitleResult]]] = {}

    def get(self, key: str) -> Optional[list[SubtitleResult]]:
        entry = self._items.get(key)
        if entry is None:
            return None
        created, results = entry
        if time.monotonic() - created > self._ttl:
            del self._items[key]
            return None
        return results

    def set(self, key: str, results: list[SubtitleResult]) -> None:
        # keep the cache bounded: drop the oldest entry when it grows too big
        if len(self._items) >= 64:
            oldest = min(self._items, key=lambda k: self._items[k][0])
            del self._items[oldest]
        self._items[key] = (time.monotonic(), results)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
