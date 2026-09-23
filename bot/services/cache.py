"""Simple TTL cache for bot hot paths — no external deps."""
import time
from typing import Any, Optional

class TTLCache:
    def __init__(self, ttl: float = 30.0, maxsize: int = 256):
        self.ttl = ttl
        self.maxsize = maxsize
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        if key in self._store:
            ts, val = self._store[key]
            if now - ts < self.ttl:
                return val
            else:
                self._store.pop(key, None)
        return None

    def set(self, key: str, value: Any) -> None:
        if len(self._store) >= self.maxsize:
            # evict oldest
            oldest = min(self._store.items(), key=lambda x: x[1][0])[0]
            self._store.pop(oldest, None)
        self._store[key] = (time.time(), value)

    def clear(self) -> None:
        self._store.clear()

# Global caches — tuned per use-case
workload_cache = TTLCache(ttl=30, maxsize=4)
manga_options_cache = TTLCache(ttl=30, maxsize=8)
webhook_creds_cache = TTLCache(ttl=60, maxsize=128)
guild_icon_cache = TTLCache(ttl=3600, maxsize=32)
drive_deep_sync_cache = TTLCache(ttl=60, maxsize=64)
user_workload_cache = TTLCache(ttl=30, maxsize=128)
