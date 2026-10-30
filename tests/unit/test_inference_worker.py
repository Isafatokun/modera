import asyncio
from threading import Event, get_ident

import pytest

from modera.errors import APIError
from modera.inference import InferenceWorker, InvalidModelOutput, ModelUnavailable
from modera.results import Finding


def test_initialization_warms_the_serving_thread_without_request_deadline() -> None:
    entered = Event()
    release = Event()

    def initialize() -> int:
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release initialization")
        return get_ident()

    async def scenario() -> None:
        worker = InferenceWorker(0.01)
        task = asyncio.create_task(worker.initialize(initialize))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(0.03)
            assert not task.done()
            with pytest.raises(APIError) as busy:
                await worker.run(lambda: [])
            assert busy.value.code == "service_busy"
            release.set()
            thread = await task

            def predict() -> list[Finding]:
                assert get_ident() == thread
                return []

            assert await worker.run(predict) == []
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            worker.close()

    asyncio.run(scenario())


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


@pytest.mark.parametrize(
    "failure,status,code,retryable",
    [
        (ValueError("private content"), 500, "model_evaluation_failed", False),
        (InvalidModelOutput("private content"), 500, "model_output_invalid", False),
        (ModelUnavailable("private content"), 503, "check_unavailable", True),
        (APIError(422, "input_too_long", "Too long."), 422, "input_too_long", False),
    ],
)
def test_inference_failure_classification(
    failure: Exception, status: int, code: str, retryable: bool
) -> None:
    def fail() -> list[Finding]:
        raise failure

    async def scenario() -> None:
        worker = InferenceWorker(1)
        try:
            with pytest.raises(APIError) as error:
                await worker.run(fail)
            assert error.value.status_code == status
            assert error.value.code == code
            assert error.value.retryable == retryable
            assert "private" not in error.value.message
            assert ("Retry-After" in (error.value.headers or {})) == retryable
            assert await worker.run(lambda: []) == []
        finally:
            worker.close()

    asyncio.run(scenario())
