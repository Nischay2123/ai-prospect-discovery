"""TTL caches and single-flight, so repeated or concurrent identical work costs one API call.

ponytail: in-process and unbounded beyond `max_items` eviction; Redis if scaled to several workers.
Nothing here is persisted, so cached personal data disappears with the process or the TTL.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from typing import Any


class TTLCache:
    def __init__(self, ttl: float, max_items: int = 1000, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._max = max_items
        self._clock = clock
        self._items: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()

    def get(self, key: Hashable) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires, value = item
        if self._clock() >= expires:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return value

    def set(self, key: Hashable, value: Any) -> None:
        self._items[key] = (self._clock() + self._ttl, value)
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)


class SingleFlight:
    """Concurrent calls with the same key share one in-flight execution and its result (or exception)."""

    def __init__(self) -> None:
        self._inflight: dict[Hashable, asyncio.Future] = {}

    async def run(self, key: Hashable, work: Callable[[], Awaitable[Any]]) -> tuple[Any, bool]:
        """(result, shared): shared is True when this caller joined someone else's execution."""
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key]), True
        future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await work()
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # mark retrieved so an unjoined failure doesn't log "never retrieved"
            raise
        else:
            future.set_result(result)
            return result, False
        finally:
            del self._inflight[key]


class Caches:
    """The app's caches, created once in the lifespan and injected (tests pass a fake clock)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.intent = TTLCache(ttl=24 * 3600, clock=clock)
        self.search = TTLCache(ttl=6 * 3600, clock=clock)
        self.page = TTLCache(ttl=6 * 3600, clock=clock)
        self.flight = SingleFlight()
