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

"""Define cache utilities."""

__all__ = ["TTLCache", "ttl_cache"]

from collections import OrderedDict
from collections.abc import Callable
from functools import wraps
from threading import RLock
from time import monotonic
from typing import Any, Generic, NamedTuple, ParamSpec, TypeVar

from pydantic import PositiveFloat, PositiveInt, validate_call

P = ParamSpec("P")
T = TypeVar("T")

_KW_MARKER = object()


def _make_key(
    *, args: tuple[Any, ...], kwargs: dict[str, Any], typed: bool
) -> tuple[Any, ...]:
    """Build a hashable cache key from function arguments.

    This roughly emulates the key strategy used by :func:`functools.lru_cache`.

    :param args: Positional arguments.
    :param kwargs: Keyword arguments.
    :param typed: If `True`, include argument types in the key.
    :return: A hashable tuple key.
    """
    if kwargs:
        items = tuple(sorted(kwargs.items()))
        key = (*args, _KW_MARKER, *items)
    else:
        items = ()
        key = args

    if typed:
        key += tuple(type(v) for v in args)
        if items:
            key += (_KW_MARKER, *((k, type(v)) for k, v in items))

    return key


class CacheInfo(NamedTuple):
    """Define structure to hold cache statistics.

    This mimics the structure used by :func:`functools.lru_cache`, adding a `ttl` field.

    :param hits: Number of cache hits.
    :param misses: Number of cache misses.
    :param maxsize: The configured maximum size of the cache (`None` means unlimited).
    :param currsize: Current number of entries stored in the cache.
    :param ttl: Time-to-live, in seconds, for each cached entry.
    """

    hits: int
    misses: int
    maxsize: int | None
    currsize: int
    ttl: float


class TTLCache(Generic[T]):
    """Encapsulate TTL cache logic with LRU eviction.

    Use it directly only when the key or the TTL cannot be derived from a
    decorated call's arguments; otherwise use :func:`ttl_cache`. A direct caller
    follows the same discipline as that wrapper: read, and later set then evict,
    each under :attr:`lock`.

    :param ttl: Time-to-live for each cached entry, in seconds.
    :param maxsize: Maximum number of entries to cache (LRU). `None` means unlimited.
    :param typed: If `True`, treat arguments with different types as distinct.
    :ivar lock: A reentrant lock to ensure thread safety.
    :ivar store: The underlying ordered dictionary used for caching.
    :ivar hits: Number of cache hits.
    :ivar misses: Number of cache misses.
    :ivar prune_limit: Maximum number of expired entries to prune in a single eviction
        cycle.
    """

    def __init__(self, *, ttl: float, maxsize: int | None, typed: bool) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self.typed = typed
        self.lock = RLock()
        self.store = OrderedDict()
        self.hits = self.misses = 0
        self.prune_limit = max(8, min(64, (maxsize or 0) // 64 or 8))

    def evict_if_needed(self, now: float) -> None:
        """Evict expired entries and enforce LRU size limit.

        This method checks the cache for expired entries and removes them. It also
        ensures that the cache does not exceed the maximum size limit. The eviction
        process is limited to a number of entries defined by `prune_limit` to avoid
        excessive performance overhead. When `maxsize` is `None` no size limit is
        applied and only expired entries are pruned, at most `prune_limit` per call.

        :param now: Current monotonic time in fractional seconds.
        """
        for _ in range(min(len(self.store), self.prune_limit)):
            _, old_expires = next(iter(self.store.values()))
            if old_expires > now:
                break
            self.store.popitem(last=False)

        if self.maxsize is None:
            return

        while len(self.store) > self.maxsize:
            self.store.popitem(last=False)

    def set(self, key: tuple[Any, ...], value: T, now: float) -> None:
        """Set a value in the cache with a TTL.

        This method sets a value in the cache with an expiration time calculated
        as the current time plus the TTL. It also moves the key to the end of the
        cache to mark it as recently used.

        :param key: Cache key.
        :param value: Value to cache.
        :param now: Current monotonic time in fractional seconds.
        """
        expires_at = now + self.ttl
        self.store[key] = (value, expires_at)
        self.store.move_to_end(key)

    def get(self, key: tuple[Any, ...], now: float) -> T:
        """Get a value from the cache, checking for expiration.

        Raises rather than returning a ``(hit, value)`` pair, so a cached value
        that is itself ``None`` stays distinguishable from a miss.

        :param key: Cache key.
        :param now: Current monotonic time in fractional seconds.
        :return: The cached value.
        :raises KeyError: When the key is absent or its entry has expired.
        """
        if key in self.store:
            value, exp = self.store[key]
            if exp > now:
                self.store.move_to_end(key)
                self.hits += 1
                return value

        self.store.pop(key, None)
        self.misses += 1
        raise KeyError(key)

    def clear(self) -> None:
        """Clear all cached entries and reset statistics."""
        with self.lock:
            self.store.clear()
            self.hits = 0
            self.misses = 0

    def info(self) -> CacheInfo:
        """Return cache statistics.

        :return: A :class:`CacheInfo` tuple with hits, misses, maxsize, currsize, ttl.
        """
        with self.lock:
            return CacheInfo(
                hits=self.hits,
                misses=self.misses,
                maxsize=self.maxsize,
                currsize=len(self.store),
                ttl=self.ttl,
            )

    def parameters(self) -> dict[str, Any]:
        """Return the cache configuration parameters.

        :return: Dictionary with `maxsize`, `typed` and `ttl`.
        """
        return {"maxsize": self.maxsize, "typed": self.typed, "ttl": self.ttl}


@validate_call
def ttl_cache(
    *,
    ttl: PositiveFloat,
    maxsize: PositiveInt | None = 128,
    typed: bool = False,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Memoize function results with a time-to-live (TTL) and LRU eviction.

    Works similarly to :func:`functools.lru_cache`, but entries automatically
    expire `ttl` seconds after being written. When an entry is expired it is
    treated as missing and recomputed on the next call.

    :param ttl: Time-to-live for each cached entry, in seconds.
    :param maxsize: Maximum number of entries to cache (LRU). `None` means unlimited.
        Defaults to `128`.
    :param typed: If `True`, treat arguments with different types as distinct. Defaults
        to `False`.
    :return: A decorator that applies a TTL/LRU cache to the target function.
    """

    def decorating_function(func: Callable[P, T]) -> Callable[P, T]:
        """Define decorator that applies TTL/LRU caching to a function.

        :param func: The function to be decorated with TTL/LRU caching.
        :return: The wrapped function with caching capabilities.
        """
        cache = TTLCache(ttl=ttl, maxsize=maxsize, typed=typed)

        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            """Define wrapped function with TTL/LRU caching."""
            key = _make_key(args=args, kwargs=kwargs, typed=typed)
            now = monotonic()
            with cache.lock:
                try:
                    return cache.get(key, now)
                except KeyError:
                    pass
            result = func(*args, **kwargs)
            now = monotonic()
            with cache.lock:
                cache.set(key, result, now)
                cache.evict_if_needed(now)
            return result

        wrapper.cache_info = cache.info
        wrapper.cache_clear = cache.clear
        wrapper.cache_parameters = cache.parameters
        return wrapper

    return decorating_function
