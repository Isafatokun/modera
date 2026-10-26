from collections.abc import AsyncIterator
from threading import Lock

from fastapi import Request

from modera.errors import APIError


class RequestCapacity:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._lock = Lock()

    def acquire(self) -> None:
        with self._lock:
            if self._active >= self._limit:
                raise APIError(
                    503,
                    "service_busy",
                    "The service is at request capacity.",
                    retryable=True,
                    headers={"Retry-After": "1"},
                )
            self._active += 1

    def release(self) -> None:
        with self._lock:
            self._active -= 1


async def admit_request(request: Request) -> AsyncIterator[None]:
    capacity: RequestCapacity = request.app.state.request_capacity
    capacity.acquire()
    try:
        yield
    finally:
        capacity.release()
