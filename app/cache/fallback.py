"""Bounded per-process fallback for transient Redis records."""

from __future__ import annotations

import time
from typing import Any


class FallbackCache:
    def __init__(self, capacity: int, ttl: int) -> None:
        self.capacity = capacity
        self.ttl = ttl
        self._rows: dict[str, tuple[float, dict[str, Any]]] = {}

    def put(self, key: str, payload: dict[str, Any]) -> None:
        self.discard(key)
        if len(self._rows) >= self.capacity:
            self.discard(next(iter(self._rows)))
        self._rows[key] = time.monotonic() + self.ttl, payload

    def get(self, key: str) -> dict[str, Any] | None:
        row = self._rows.get(key)
        if row is not None:
            if time.monotonic() < row[0]:
                return row[1]
            self.discard(key)
        return None

    def discard(self, key: str) -> None:
        self._rows.pop(key, None)
