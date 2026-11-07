"""Small in-process caching helpers shared by every module.

* ``TTLCache``   -- bounded LRU with per-entry expiry. When it is full it evicts the *oldest* entry, instead of the
                    old ``dict.clear()`` pattern that threw away every hot entry at once and caused a stampede of
                    upstream calls right after.
* ``singleflight`` -- when N identical requests arrive while the first is still in flight, they all await the same
                    upstream call instead of making N of them. Cuts upstream load and tail latency under bursts.
"""
import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Hashable

_MISS = object()


class TTLCache:
    def __init__(self, maxsize: int = 500, ttl: float = 600):
        self.maxsize = maxsize
        self.ttl = ttl
        self._d: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()

    def get(self, key: Hashable, default: Any = None) -> Any:
        item = self._d.get(key)
        if item is None:
            return default
        if item[0] <= time.monotonic():
            self._d.pop(key, None)
            return default
        self._d.move_to_end(key)
        return item[1]

    def set(self, key: Hashable, value: Any, ttl: float | None = None) -> Any:
        self._d[key] = (time.monotonic() + (self.ttl if ttl is None else ttl), value)
        self._d.move_to_end(key)
        while len(self._d) > self.maxsize:
            self._d.popitem(last=False)
        return value

    def __len__(self) -> int:
        return len(self._d)

    def clear(self) -> None:
        self._d.clear()


_inflight: dict[Hashable, asyncio.Future] = {}


async def singleflight(key: Hashable, make: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``make()`` once per key at a time; concurrent callers share the result (or the exception).

    The leader runs ``make`` in its own task so a client that disconnects does not cancel the work for everyone else.
    """
    fut = _inflight.get(key)
    if fut is not None:
        return await asyncio.shield(fut)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _inflight[key] = fut

    async def run():
        try:
            fut.set_result(await make())
        except BaseException as e:  # noqa: BLE001 - delivered to every waiter
            fut.set_exception(e)
            fut.exception()  # mark retrieved so an unawaited failure is not logged twice
        finally:
            _inflight.pop(key, None)

    asyncio.ensure_future(run())
    return await asyncio.shield(fut)
