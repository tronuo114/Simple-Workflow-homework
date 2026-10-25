# 任务：实现一个「带过期时间的 LRU 缓存」

## 背景

服务端需要一个进程内缓存：容量固定，每个条目有存活时间（TTL），读的时候才判断过期。
业务侧要求它能在多线程环境下直接用，且不能因为条目多了就变慢。

## 接口

```python
class TTLCache:
    def __init__(self, capacity: int, ttl_seconds: float): ...
    def get(self, key) -> Any: ...      # 不存在或已过期返回 None
    def put(self, key, value) -> None:  # 写入/覆盖，超容量淘汰最久未使用
```

## 要求

1. 容量满时淘汰最久未使用的条目（LRU）。
2. 每个条目有 TTL；`get` 时如果已过期，视为不存在，且该条目应被移除。
3. `put` 覆盖已存在的 key 时，要刷新 TTL。
4. 过期条目不应长期占用容量（惰性删除 + 容量满时优先清理过期）。
5. 线程安全。
6. 时间复杂度：`get` / `put` 均摊 O(1)。

## 交付

单文件模块 `ttl_cache.py`，不引入第三方依赖，Python 3.9+ 可运行。
