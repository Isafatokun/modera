import asyncio
from pathlib import Path
from threading import Event

import pytest
from httpx2 import ASGITransport, AsyncClient, Response

from modera import engine
from modera.config import Settings
from modera.main import create_app
from modera.model_config import ImageModelSettings, TextModelSettings
from modera.results import Finding


@pytest.mark.parametrize("expire", [False, True])
def test_photo_burst_waits_or_returns_an_explicit_retryable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    image_bytes: bytes,
    expire: bool,
) -> None:
    entered, release = Event(), Event()
    calls: list[int] = []

    class BlockingClassifier:
        def __init__(
            self, settings: TextModelSettings | ImageModelSettings, threads: int
        ) -> None:
            pass

        def predict(self, content: str | bytes) -> list[Finding]:
            calls.append(1)
            entered.set()
            assert release.wait(5)
            return [Finding(category="sexual", score=0.1)]

    monkeypatch.setattr(engine, "Classifier", BlockingClassifier)
    policy = tmp_path / "policy.json"
    policy.write_text(
        '{"policies":[{"id":"default","version":"1",'
        '"image":[{"category":"sexual","block_threshold":0.8}]}]}'
    )
    app = create_app(
        Settings(
            policy_file=policy,
            image_model=ImageModelSettings(
                id="example/image", revision="a" * 40, label_map={"nsfw": "sexual"}
            ),
            image_queue_wait_seconds=0.05 if expire else 2,
        )
    )

    async def run() -> None:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers=auth_headers,
            ) as client,
        ):

            async def request() -> Response:
                return await client.post(
                    "/v1/moderations", files={"file": ("photo.png", image_bytes)}
                )

            first = asyncio.create_task(request())
            pending = []
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                if expire:
                    response = await request()
                    assert response.status_code == 503
                    assert response.json()["error"]["code"] == "queue_wait_timeout"
                    assert response.json()["error"]["retryable"]
                    assert response.headers["Retry-After"] == "1"
                    assert "decision" not in response.json()
                else:
                    pending = [asyncio.create_task(request()) for _ in range(5)]
                    await asyncio.sleep(0.03)
                    assert not any(task.done() for task in pending)
                assert (await client.get("/health/ready")).status_code == 200
                assert (await client.get("/metrics")).status_code == 200
                release.set()
                for response in await asyncio.gather(first, *pending):
                    assert response.status_code == 200
                    assert response.json()["decision"]["action"] == "allow"
                    assert not response.json()["fallback"]
                assert len(calls) == (1 if expire else 6)
                assert (await request()).status_code == 200
            finally:
                release.set()
                await asyncio.gather(first, *pending, return_exceptions=True)

    asyncio.run(run())
