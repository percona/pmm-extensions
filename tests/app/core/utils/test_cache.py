# Copyright (C) 2026 Percona LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Tests for :mod:`app.core.utils.cache`."""

from app.core.utils.cache import ttl_cache, TTLCache

PRUNE_BUDGET = 8
EXPIRED_ENTRIES = 12


def test_unlimited_cache_keeps_live_entry() -> None:
    """Keep a live entry through an eviction pass when ``maxsize`` is ``None``."""
    cache = TTLCache(ttl=60, maxsize=None, typed=False)
    cache.set(("a",), 1, 0.0)
    cache.evict_if_needed(1.0)
    assert len(cache.store) == 1


def test_unlimited_cache_prunes_expired_within_budget() -> None:
    """Prune one budget of expired entries per pass when ``maxsize`` is ``None``."""
    cache = TTLCache(ttl=1, maxsize=None, typed=False)
    assert cache.prune_limit == PRUNE_BUDGET
    for i in range(EXPIRED_ENTRIES):
        cache.set((i,), i, 0.0)
    cache.evict_if_needed(10.0)
    assert len(cache.store) == EXPIRED_ENTRIES - PRUNE_BUDGET


def test_bounded_cache_evicts_least_recently_used() -> None:
    """Evict the entry read longest ago once an integer ``maxsize`` is exceeded."""
    cache = TTLCache(ttl=60, maxsize=2, typed=False)
    cache.set((0,), 0, 0.0)
    cache.set((1,), 1, 0.0)
    cache.get((0,), 0.5)
    cache.set((2,), 2, 1.0)
    cache.evict_if_needed(1.0)
    assert list(cache.store) == [(0,), (2,)]


def test_ttl_cache_unlimited_returns_cached_result() -> None:
    """Return the cached result on a repeated call when ``maxsize`` is ``None``."""
    calls: list[int] = []

    @ttl_cache(ttl=60, maxsize=None)
    def func(x: int) -> int:
        calls.append(x)
        return x * 2

    assert func(1) == func(1)
    assert calls == [1]
    info = func.cache_info()
    assert info.hits == 1
    assert info.currsize == 1
