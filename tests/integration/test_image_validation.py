import asyncio
import logging
from threading import Event

import pytest
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient

from modera import images
from modera.main import create_app


def test_filename_and_mime_are_not_trusted(
    auth_headers: dict[str, str], image_bytes: bytes
) -> None:
    with TestClient(create_app(), headers=auth_headers) as client:
        response = client.post(
            "/v1/moderations",
            files={"file": ("misleading.svg", image_bytes, "text/plain")},
        )
    assert response.status_code == 501


def test_invalid_image_returns_sanitized_error(
    auth_headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.DEBUG),
        TestClient(create_app(), headers=auth_headers) as client,
    ):
        response = client.post(
            "/v1/moderations",
            files={
                "file": (
                    "secret-filename.jpg",
                    b"\xff\xd8\xffsecret-content",
                    "image/jpeg",
                )
            },
        )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_image"
    assert error["param"] == "file"
    assert error["retryable"] is False
    for secret in ("secret-filename", "secret-content"):
        assert secret not in response.text + caplog.text


def test_overload_does_not_block_health_or_queue_decodes(
    monkeypatch: pytest.MonkeyPatch, auth_headers: dict[str, str], image_bytes: bytes
) -> None:
    started, finish = Event(), Event()

    def blocked(data: bytes) -> None:
        started.set()
        if not finish.wait(5):
            raise RuntimeError("test worker was not released")

    monkeypatch.setattr(images, "validate_image", blocked)

    async def scenario() -> None:
        app = create_app()
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
                headers=auth_headers,
            ) as client,
        ):
            first = asyncio.create_task(
                client.post(
                    "/v1/moderations", files={"file": ("photo.png", image_bytes)}
                )
            )
            try:
                assert await asyncio.to_thread(started.wait, 2)
                assert (await client.get("/health/live")).status_code == 200
                response = await client.post(
                    "/v1/moderations", files={"file": ("photo.png", image_bytes)}
                )
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "service_busy"
                assert response.headers["Retry-After"] == "1"
            finally:
                finish.set()
            assert (await first).status_code == 501
            assert (
                await client.post(
                    "/v1/moderations", files={"file": ("photo.png", image_bytes)}
                )
            ).status_code == 501

    asyncio.run(scenario())


def test_lifespan_restarts_resources(
    auth_headers: dict[str, str], image_bytes: bytes
) -> None:
    app = create_app()
    for _ in range(2):
        with TestClient(app, headers=auth_headers) as client:
            response = client.post(
                "/v1/moderations", files={"file": ("photo.png", image_bytes)}
            )
            assert response.status_code == 501
