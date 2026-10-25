"""TTLCache 验收测试。

覆盖题目 6 条要求：
REQ-1 LRU 淘汰 / REQ-2 TTL 过期与惰性删除 / REQ-3 put 刷新 TTL /
REQ-4 过期不占容量 / REQ-5 线程安全 / REQ-6 均摊 O(1)

时间用注入的假时钟控制，不依赖 sleep，测试稳定可重复；
另有一个真实 sleep 的冒烟用例兜底「真时钟也能过期」。
"""

from __future__ import annotations

import threading
import time

import pytest

from ttl_cache import TTLCache


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def cache(clock: FakeClock) -> TTLCache:
    return TTLCache(capacity=3, ttl_seconds=10.0, clock=clock)


# --------------------------------------------------------------------------- #
# 构造参数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("capacity,ttl", [(0, 1.0), (-1, 1.0), (2, 0), (2, -0.5)])
def test_reject_invalid_arguments(capacity, ttl) -> None:
    with pytest.raises(ValueError):
        TTLCache(capacity, ttl)


def test_reject_non_int_capacity() -> None:
    with pytest.raises(TypeError):
        TTLCache(2.5, 1.0)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# REQ-2 / 基本读写
# --------------------------------------------------------------------------- #
def test_put_then_get(cache: TTLCache) -> None:
    cache.put("a", 1)
    assert cache.get("a") == 1


def test_missing_key_returns_none(cache: TTLCache) -> None:
    assert cache.get("nope") is None
    assert cache.get("nope", "fallback") == "fallback"


def test_expired_entry_is_miss_and_removed(cache, clock) -> None:
    cache.put("a", 1)
    clock.advance(9.9)
    assert cache.get("a") == 1
    clock.advance(0.2)          # 累计超过 ttl
    assert cache.get("a") is None
    assert "a" not in cache
    assert len(cache) == 0      # 惰性删除已经把它拿走


def test_entry_expires_exactly_at_ttl(cache, clock) -> None:
    cache.put("a", 1)
    clock.advance(10.0)
    assert cache.get("a") is None      # 到期时刻即视为过期


# --------------------------------------------------------------------------- #
# REQ-3 put 覆盖刷新 TTL
# --------------------------------------------------------------------------- #
def test_put_existing_refreshes_ttl(cache, clock) -> None:
    cache.put("k", "v1")
    clock.advance(9.0)
    cache.put("k", "v2")               # 覆盖 -> 重新计时
    clock.advance(9.0)                 # 距首次写入 18s，距覆盖 9s
    assert cache.get("k") == "v2"
    clock.advance(1.1)
    assert cache.get("k") is None


def test_overwrite_does_not_evict_others(cache, clock) -> None:
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    for i in range(5):
        cache.put("a", i)              # 反复覆盖同一个 key，不应触发淘汰
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    assert len(cache) == 3


# --------------------------------------------------------------------------- #
# REQ-1 LRU
# --------------------------------------------------------------------------- #
def test_evicts_least_recently_used(cache) -> None:
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    cache.put("d", 4)                  # 超容量 -> 淘汰最久未使用的 a
    assert cache.get("a") is None
    assert [cache.get(k) for k in ("b", "c", "d")] == [2, 3, 4]


def test_get_updates_recency(cache) -> None:
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") == 1          # a 变成最近使用
    cache.put("d", 4)                   # 应淘汰 b
    assert cache.get("b") is None
    assert cache.get("a") == 1


def test_capacity_one(cache) -> None:
    one = TTLCache(1, 5.0)
    one.put("a", 1)
    one.put("b", 2)
    assert one.get("a") is None
    assert one.get("b") == 2


# --------------------------------------------------------------------------- #
# REQ-4 过期不长期占容量，容量满时优先清理过期
# --------------------------------------------------------------------------- #
def test_expired_entries_do_not_block_new_writes(cache, clock) -> None:
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    clock.advance(10.5)                # 三条全部过期，但尚未被 get 触碰
    cache.put("d", 4)                  # 容量满 -> 应先清过期，而不是淘汰 LRU
    assert len(cache) == 1
    assert cache.get("d") == 4
    assert cache.stats()["evictions"] == 0     # 有效条目一条都没被牺牲


def test_purge_expired_is_optional_but_works(cache, clock) -> None:
    cache.put("a", 1)
    cache.put("b", 2)
    clock.advance(10.5)
    assert cache.purge_expired() == 2
    assert len(cache) == 0
    assert cache.purge_expired() == 0


def test_mixed_expiry_keeps_live_entries(cache, clock) -> None:
    cache.put("old", 1)
    clock.advance(6.0)
    cache.put("new", 2)
    clock.advance(5.0)                 # old 已过期，new 还剩 5s
    cache.put("x", 3)
    assert cache.get("old") is None
    assert cache.get("new") == 2
    assert cache.get("x") == 3


# --------------------------------------------------------------------------- #
# REQ-5 线程安全
# --------------------------------------------------------------------------- #
def test_concurrent_read_write_does_not_break_invariants() -> None:
    cache = TTLCache(capacity=50, ttl_seconds=0.05)
    errors: list[BaseException] = []

    def worker(seed: int) -> None:
        try:
            for i in range(800):
                key = (seed * 7 + i) % 120
                if i % 3 == 0:
                    cache.put(key, i)
                else:
                    cache.get(key)
                if i % 97 == 0:
                    cache.purge_expired()
        except BaseException as exc:  # noqa: BLE001 - 线程里任何异常都要传出来
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads), "存在死锁：线程未退出"
    assert not errors, f"并发下抛异常: {errors[:3]}"
    assert len(cache) <= 50, "超出容量上限"


def test_concurrent_put_never_exceeds_capacity() -> None:
    cache = TTLCache(capacity=20, ttl_seconds=60.0)
    barrier = threading.Barrier(6)

    def worker(tid: int) -> None:
        barrier.wait()
        for i in range(300):
            cache.put((tid, i), i)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(cache) <= 20


# --------------------------------------------------------------------------- #
# REQ-6 复杂度 / 结构健壮性
# --------------------------------------------------------------------------- #
def test_put_many_with_small_capacity_is_fast() -> None:
    """容量固定时，写入耗时应与历史写入总量无关（近似常数）。"""
    def bench(capacity: int, ops: int = 4000) -> float:
        c = TTLCache(capacity=capacity, ttl_seconds=3600.0)
        start = time.perf_counter()
        for i in range(ops):
            c.put(i, i)
            c.get(i)
        return time.perf_counter() - start

    small = bench(8)
    large = bench(20000)
    # 宽松上限：线性扫描实现会差几个数量级
    assert large < max(small, 0.02) * 8, f"小容量 {small:.4f}s vs 大容量 {large:.4f}s"


def test_expiry_queue_stays_bounded() -> None:
    cache = TTLCache(capacity=10, ttl_seconds=3600.0)
    for i in range(5000):
        cache.put("k", i)              # 反复覆盖同一个 key，制造陈旧记录
    assert len(cache._expiry) <= 2 * 10 + 32 + 1


def test_stats_and_items(cache, clock) -> None:
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")
    cache.get("zzz")
    items = cache.items()
    assert sorted(k for k, _ in items) == ["a", "b"]
    stats = cache.stats()
    assert stats["hits"] == 1 and stats["misses"] == 1
    assert stats["size"] == 2 and stats["capacity"] == 3
    clock.advance(11)
    assert cache.items() == []


def test_none_value_is_storable(cache) -> None:
    cache.put("k", None)
    assert cache.get("k") is None
    assert "k" in cache              # contains 可区分「不存在」与「值是 None」


def test_clear(cache) -> None:
    cache.put("a", 1)
    cache.clear()
    assert len(cache) == 0
    assert cache.get("a") is None


# --------------------------------------------------------------------------- #
# 真时钟冒烟
# --------------------------------------------------------------------------- #
def test_real_clock_expiry() -> None:
    cache = TTLCache(capacity=2, ttl_seconds=0.05)
    cache.put("k", "v")
    assert cache.get("k") == "v"
    time.sleep(0.08)
    assert cache.get("k") is None
