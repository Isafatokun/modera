import asyncio
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from typing import NamedTuple

from modera.errors import APIError


class WorkerCapacity(NamedTuple):
    active: int
    waiting: int


def unavailable(code: str = "service_busy") -> APIError:
    return APIError(
        503,
        code,
        "Worker capacity is temporarily unavailable.",
        retryable=True,
        headers={"Retry-After": "1"},
    )


class BoundedWorker:
    """One native job and a bounded FIFO owned by the serving event loop."""

    def __init__(self, name: str, queue_size: int = 0, wait_seconds: float = 2) -> None:
        if queue_size < 0 or wait_seconds <= 0:
            raise ValueError("invalid worker queue limits")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._queue_size = queue_size
        self._wait_seconds = wait_seconds
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active = False
        self._closed = False

    @property
    def capacity(self) -> WorkerCapacity:
        return WorkerCapacity(int(self._active), len(self._waiters))

    def _release(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return
        self._active = False

    async def _acquire(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("worker must be used on one event loop")
        if self._closed:
            raise unavailable()
        if not self._active:
            self._active = True
            return
        if len(self._waiters) >= self._queue_size:
            raise unavailable()
        waiter = loop.create_future()
        self._waiters.append(waiter)
        try:
            async with asyncio.timeout(self._wait_seconds):
                await asyncio.shield(waiter)
        except BaseException as exc:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            elif (
                waiter.done() and not waiter.cancelled() and waiter.exception() is None
            ):
                self._release()
            waiter.cancel()
            if isinstance(exc, TimeoutError):
                raise unavailable("queue_wait_timeout") from exc
            raise
        if self._closed:
            self._release()
            raise unavailable()

    async def run[T](self, operation: Callable[[], T]) -> T:
        await self._acquire()
        loop = asyncio.get_running_loop()
        try:
            future = self._executor.submit(operation)
        except BaseException:
            self._release()
            raise

        def completed(_future: Future[T]) -> None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._release)

        future.add_done_callback(completed)
        return await asyncio.wrap_future(future)

    def close(self) -> None:
        self._closed = True
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_exception(unavailable())
        self._executor.shutdown(wait=False, cancel_futures=True)
