import asyncio
from collections import Counter
from pathlib import Path
from threading import Event, get_ident
from typing import Literal

import pytest
from httpx2 import ASGITransport, AsyncClient, Response

from modera import engine
from modera.config import Settings
from modera.main import create_app
from modera.model_config import ImageModelSettings, TextModelSettings
from modera.policies import InputType
from modera.results import Finding


@pytest.mark.parametrize("blocked_kind", ["text", "image"])
@pytest.mark.parametrize("outcome", ["busy", "timeout", "cancel"])
def test_modalities_have_independent_bounded_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    image_bytes: bytes,
    blocked_kind: InputType,
    outcome: Literal["busy", "timeout", "cancel"],
) -> None:
    entered = Event()
    release = Event()
    loads: Counter[str] = Counter()

    class BlockingClassifier:
        def __init__(
            self, settings: TextModelSettings | ImageModelSettings, threads: int
        ) -> None:
            self.kind = "text" if isinstance(settings, TextModelSettings) else "image"
            self.category = next(iter(settings.label_map.values()))
            self.thread = get_ident()
            loads[self.kind] += 1

        def predict(self, content: str | bytes) -> list[Finding]:
            assert get_ident() == self.thread
            if self.kind == blocked_kind:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("test did not release inference")
            return [Finding(category=self.category, score=0.1)]

    monkeypatch.setattr(engine, "Classifier", BlockingClassifier)
    policy = tmp_path / "policies.json"
    policy.write_text(
        '{"policies":[{"id":"default","version":"1",'
        '"text":[{"category":"toxicity","block_threshold":0.8}],'
        '"image":[{"category":"sexual","block_threshold":0.8}]}]}'
    )
    app = create_app(
        Settings(
            policy_file=policy,
            text_model=TextModelSettings(
                id="example/text", revision="a" * 40, label_map={"toxic": "toxicity"}
            ),
            image_model=ImageModelSettings(
                id="example/image", revision="b" * 40, label_map={"nsfw": "sexual"}
            ),
            inference_timeout_seconds=0.3 if outcome == "timeout" else 5,
            image_queue_size=0,
        )
    )

    async def scenario() -> None:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
                headers=auth_headers,
            ) as client,
        ):

            async def request(kind: InputType) -> Response:
                if kind == "text":
                    return await client.post(
                        "/v1/moderations",
                        json={"input": {"type": "text", "text": "hello"}},
                    )
                return await client.post(
                    "/v1/moderations", files={"file": ("photo.png", image_bytes)}
                )

            first = asyncio.create_task(request(blocked_kind))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                if outcome == "timeout":
                    response = await first
                    assert response.status_code == 503
                    assert response.json()["error"]["code"] == "check_unavailable"
                elif outcome == "cancel":
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                other_kind: InputType = "image" if blocked_kind == "text" else "text"
                for _ in range(2):
                    response = await request(other_kind)
                    assert response.status_code == 200
                    assert response.json()["models"][0]["id"] == f"example/{other_kind}"
                    assert not response.json()["fallback"]
                    response = await request(blocked_kind)
                    assert response.status_code == 503
                    assert response.json()["error"]["code"] == "service_busy"
                    assert response.headers["Retry-After"] == "1"
                assert (await client.get("/health/ready")).status_code == 200
                release.set()
                if outcome == "busy":
                    assert (await first).status_code == 200
                for _ in range(100):
                    response = await request(blocked_kind)
                    if response.status_code == 200:
                        break
                    assert response.json()["error"]["code"] == "service_busy"
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("native inference capacity did not recover")
                assert loads == {"text": 1, "image": 1}
            finally:
                release.set()
                if not first.done():
                    first.cancel()
                await asyncio.gather(first, return_exceptions=True)

    asyncio.run(scenario())
