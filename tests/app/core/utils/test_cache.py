"""Tests for :mod:`app.core.utils.cache`."""

from app.core.utils.cache import TTLCache, ttl_cache


def test_unlimited_cache_keeps_live_entry() -> None:
    cache = TTLCache(ttl=60, maxsize=None, typed=False)
    cache.set(("a",), 1, 0.0)
    cache.evict_if_needed(1.0)
    assert len(cache.store) == 1


def test_unlimited_cache_prunes_expired_within_budget() -> None:
    cache = TTLCache(ttl=1, maxsize=None, typed=False)
    assert cache.prune_limit == 8
    for i in range(12):
        cache.set((i,), i, 0.0)
    cache.evict_if_needed(10.0)
    assert len(cache.store) == 4


def test_bounded_cache_evicts_least_recently_used() -> None:
    cache = TTLCache(ttl=60, maxsize=2, typed=False)
    for i in range(3):
        cache.set((i,), i, 0.0)
    cache.evict_if_needed(1.0)
    assert list(cache.store) == [(1,), (2,)]


def test_ttl_cache_unlimited_returns_cached_result() -> None:
    calls = []

    @ttl_cache(ttl=60, maxsize=None)
    def func(x: int) -> int:
        calls.append(x)
        return x * 2

    assert func(1) == func(1) == 2
    assert calls == [1]
    info = func.cache_info()
    assert info.hits == 1
    assert info.currsize == 1
