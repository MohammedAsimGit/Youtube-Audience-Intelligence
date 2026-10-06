"""Development in-memory TTL/LRU cache - the cache BOUNDARY, not the store.

Sprint 2 introduces the lookup seam required by the architecture:

    request -> cache lookup -> hit? return : YouTube API -> populate

Persistent storage/caching (DB + Redis-class infra) is a later sprint; this
class only proves the seam and saves quota during development. Thread-safe
for FastAPI's threadpool handlers.
"""
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional


class TTLCache:
    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if self._clock() >= expires_at:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl_seconds: Optional[float] = None) -> None:
        ttl = self._ttl if ttl_seconds is None else ttl_seconds
        with self._lock:
            self._entries[key] = (self._clock() + ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)  # LRU eviction

    def clear(self) -> None:
        """Drop every entry (Sprint 4.2: L1 mirrors the single-active-video
        dataset policy - a video switch never leaves old datasets in memory)."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
