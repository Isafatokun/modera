import asyncio
from threading import Event

import pytest

from modera.errors import APIError
from modera.inference import InferenceWorker
from modera.results import Finding


def test_inference_timeout_keeps_capacity_until_native_work_finishes() -> None:
    entered = Event()
    release = Event()

    def operation() -> list[Finding]:
        entered.set()
        release.wait(5)
        return [Finding(category="toxicity", score=0.5)]

    async def scenario() -> None:
        worker = InferenceWorker(0.02)
        try:
            with pytest.raises(APIError) as error:
                await worker.run(operation)
            assert entered.is_set()
            assert error.value.code == "check_unavailable"
            with pytest.raises(APIError) as busy:
                await worker.run(operation)
            assert busy.value.code == "service_busy"
            release.set()
            for _ in range(100):
                await asyncio.sleep(0.01)
                try:
                    assert (await worker.run(lambda: [])) == []
                    break
                except APIError as exc:
                    assert exc.code == "service_busy"
            else:
                pytest.fail("inference capacity did not recover")
        finally:
            release.set()
            worker.close()

    asyncio.run(scenario())


def test_inference_cancellation_retains_capacity_and_errors_are_sanitized() -> None:
    async def scenario() -> None:
        entered = Event()
        release = Event()
        worker = InferenceWorker(5)

        def blocked() -> list[Finding]:
            entered.set()
            release.wait(5)
            raise RuntimeError("private input and URL")

        task = asyncio.create_task(worker.run(blocked))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(APIError) as error:
                await worker.run(lambda: [])
            assert error.value.code == "service_busy"
        finally:
            release.set()
            worker.close()
        with pytest.raises(APIError):
            await worker.run(lambda: [])

    asyncio.run(scenario())
