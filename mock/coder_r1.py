"""TTLCache —— 第一版（mock 场景里的「编码 Agent 初稿」，故意留下典型缺陷）。"""

import time
from typing import Any, Dict, Tuple


class TTLCache:
    def __init__(self, capacity: int, ttl_seconds: float):
        self.capacity = capacity
        self.ttl = ttl_seconds
        self._store: Dict[Any, Tuple[Any, float]] = {}
        self._last_used: Dict[Any, float] = {}

    def get(self, key):
        item = self._store.get(key)
        if item is None:
            return None
        value, expire_at = item
        if time.time() >= expire_at:
            return None                      # 过期了，但没删掉
        self._last_used[key] = time.time()
        return value

    def put(self, key, value) -> None:
        if key in self._store:
            _, expire_at = self._store[key]
            self._store[key] = (value, expire_at)   # 覆盖时忘了刷新 TTL
        else:
            if len(self._store) >= self.capacity:
                # 线性扫描找最久未使用的 key
                oldest = min(self._store, key=lambda k: self._last_used.get(k, 0.0))
                self._store.pop(oldest, None)
                self._last_used.pop(oldest, None)
            self._store[key] = (value, time.time() + self.ttl)
        self._last_used[key] = time.time()
