import asyncio
from collections.abc import AsyncIterator

import pytest
from httpx2 import ASGITransport, AsyncClient

from modera.config import Settings
from modera.main import create_app


@pytest.mark.parametrize("cancel", [False, True])
def test_request_capacity_covers_body_reading_and_recovers(
    auth_headers: dict[str, str], cancel: bool
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        app = create_app(Settings(max_in_flight_requests=1))

        async def body() -> AsyncIterator[bytes]:
            entered.set()
            yield b'{"input":'
            await release.wait()
            yield b'{"type":"text","text":"hello"}}'

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
                    "/v1/moderations",
                    content=body(),
                    headers={"Content-Type": "application/json"},
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                consumed = False

                async def extra_body() -> AsyncIterator[bytes]:
                    nonlocal consumed
                    consumed = True
                    yield b"private input"

                response = await client.post(
                    "/v1/moderations",
                    content=extra_body(),
                    headers={"Content-Type": "application/json"},
                )
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "service_busy"
                assert not consumed
                assert (await client.get("/health/live")).status_code == 200
                denied = await client.post(
                    "/v1/moderations",
                    content=b"bad",
                    headers={"Authorization": "Bearer invalid"},
                )
                assert denied.status_code == 401
                if cancel:
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                else:
                    release.set()
                    assert (await first).status_code == 501
                response = await client.post(
                    "/v1/moderations", json={"input": {"type": "text", "text": "hello"}}
                )
                assert response.status_code == 501
            finally:
                release.set()
                if not first.done():
                    first.cancel()

    asyncio.run(scenario())
