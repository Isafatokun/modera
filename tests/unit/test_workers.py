import asyncio
from functools import partial
from threading import Event

import pytest

from modera.errors import APIError
from modera.workers import BoundedWorker


def test_fifo_is_bounded_and_pending_work_is_not_executed() -> None:
    async def run() -> None:
        started, release = Event(), Event()
        order: list[str] = []
        worker = BoundedWorker("test", 2)

        def first() -> str:
            started.set()
            assert release.wait(5)
            order.append("first")
            return "first"

        def next_job(name: str) -> str:
            order.append(name)
            return name

        task = asyncio.create_task(worker.run(first))
        pending: list[asyncio.Task[str]] = []
        try:
            assert await asyncio.to_thread(started.wait, 2)
            for name in ("second", "third"):
                pending.append(asyncio.create_task(worker.run(partial(next_job, name))))
            await asyncio.sleep(0)
            assert order == []
            with pytest.raises(APIError) as error:
                await worker.run(lambda: next_job("overflow"))
            assert error.value.code == "service_busy"
            release.set()
            assert await asyncio.gather(task, *pending) == ["first", "second", "third"]
            assert order == ["first", "second", "third"]
        finally:
            release.set()
            await asyncio.gather(task, *pending, return_exceptions=True)
            worker.close()

    asyncio.run(run())


@pytest.mark.parametrize("cancelled_index", [0, 1])
def test_cancelled_waiter_is_removed_and_never_runs(cancelled_index: int) -> None:
    async def run() -> None:
        started, release = Event(), Event()
        executed: list[str] = []
        worker = BoundedWorker("test", 2)

        def first() -> None:
            started.set()
            assert release.wait(5)

        task = asyncio.create_task(worker.run(first))
        pending: list[asyncio.Task[None]] = []
        try:
            assert await asyncio.to_thread(started.wait, 2)
            for name in ("second", "third"):
                pending.append(
                    asyncio.create_task(worker.run(partial(executed.append, name)))
                )
            await asyncio.sleep(0)
            pending[cancelled_index].cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending[cancelled_index]
            pending.append(
                asyncio.create_task(worker.run(lambda: executed.append("fourth")))
            )
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(task, *pending, return_exceptions=True)
            assert executed == ["third" if cancelled_index == 0 else "second", "fourth"]
        finally:
            release.set()
            await asyncio.gather(task, *pending, return_exceptions=True)
            worker.close()

    asyncio.run(run())


def test_expired_waiter_never_executes_and_capacity_recovers() -> None:
    async def run() -> None:
        started, release, expired = Event(), Event(), Event()
        worker = BoundedWorker("test", 1, 0.02)

        def first() -> None:
            started.set()
            assert release.wait(5)

        task = asyncio.create_task(worker.run(first))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            with pytest.raises(APIError) as error:
                await worker.run(expired.set)
            assert error.value.code == "queue_wait_timeout"
            assert error.value.retryable
            release.set()
            await task
            assert await worker.run(lambda: "recovered") == "recovered"
            assert not expired.is_set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            worker.close()

    asyncio.run(run())


def test_running_cancellation_does_not_start_the_next_native_job() -> None:
    async def run() -> None:
        started, release, next_started = Event(), Event(), Event()
        worker = BoundedWorker("test", 1)

        def first() -> None:
            started.set()
            assert release.wait(5)

        task = asyncio.create_task(worker.run(first))
        pending: asyncio.Task[None] | None = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            pending = asyncio.create_task(worker.run(next_started.set))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not next_started.is_set()
            with pytest.raises(APIError):
                await worker.run(lambda: None)
            release.set()
            await pending
            assert next_started.is_set()
        finally:
            release.set()
            await asyncio.gather(
                task, *([pending] if pending else []), return_exceptions=True
            )
            worker.close()

    asyncio.run(run())


def test_shutdown_rejects_waiters_and_new_work_without_killing_active_work() -> None:
    async def run() -> None:
        started, release, queued = Event(), Event(), Event()
        worker = BoundedWorker("test", 1)

        def first() -> str:
            started.set()
            assert release.wait(5)
            return "finished"

        task = asyncio.create_task(worker.run(first))
        pending: asyncio.Task[None] | None = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            pending = asyncio.create_task(worker.run(queued.set))
            await asyncio.sleep(0)
            worker.close()
            worker.close()
            with pytest.raises(APIError):
                await pending
            with pytest.raises(APIError):
                await worker.run(queued.set)
            assert not queued.is_set()
            release.set()
            assert await task == "finished"
        finally:
            release.set()
            await asyncio.gather(
                task, *([pending] if pending else []), return_exceptions=True
            )
            worker.close()

    asyncio.run(run())


def test_cancellation_after_grant_passes_the_slot_to_the_next_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        started, release, cancelled_job = Event(), Event(), Event()
        worker = BoundedWorker("test", 2)

        def first() -> None:
            started.set()
            assert release.wait(5)

        active = asyncio.create_task(worker.run(first))
        pending: list[asyncio.Task[object]] = []
        try:
            assert await asyncio.to_thread(started.wait, 2)
            cancelled = asyncio.create_task(worker.run(cancelled_job.set))
            next_job = asyncio.create_task(worker.run(lambda: "next"))
            pending = [cancelled, next_job]
            await asyncio.sleep(0)
            original_release = worker._release
            armed = True

            def interrupt_grant() -> None:
                nonlocal armed
                original_release()
                if armed:
                    armed = False
                    cancelled.cancel()

            monkeypatch.setattr(worker, "_release", interrupt_grant)
            release.set()
            await active
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert await next_job == "next"
            assert not cancelled_job.is_set()
            assert await worker.run(lambda: "available") == "available"
        finally:
            release.set()
            await asyncio.gather(active, *pending, return_exceptions=True)
            worker.close()

    asyncio.run(run())
