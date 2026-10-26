import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient
from prometheus_client.parser import text_string_to_metric_families

from modera.config import Settings
from modera.main import create_app


def sample(text: str, name: str, labels: dict[str, str] | None = None) -> float:
    for family in text_string_to_metric_families(text):
        for measurement in family.samples:
            if measurement.name == name and measurement.labels == (labels or {}):
                return measurement.value
    raise AssertionError(f"Missing metric {name} {labels}")


def test_metrics_require_authentication_and_are_isolated_per_app(
    auth_headers: dict[str, str],
) -> None:
    with TestClient(create_app()) as client:
        assert client.get("/metrics").status_code == 401
        client.post("/v1/moderations", content="private submitted text")
        client.get("/unknown/private-path")
        response = client.get("/metrics", headers=auth_headers)
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("text/plain;")
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Request-ID"].startswith("req_")
        text = response.text
        assert (
            sample(text, "modera_moderation_requests_total", {"status_code": "401"})
            == 1
        )
        assert sample(text, "modera_moderation_request_duration_seconds_count") == 1
        assert sample(text, "modera_moderation_requests_in_flight") == 0
        for private in [
            "private",
            auth_headers["Authorization"],
            "req_",
            "python_info",
        ]:
            assert private not in text
    with TestClient(create_app(), headers=auth_headers) as client:
        text = client.get("/metrics").text
        assert sample(text, "modera_moderation_request_duration_seconds_count") == 0
        assert 'status_code="401"' not in text


def test_concurrent_overload_is_observable_and_recovers(
    auth_headers: dict[str, str],
) -> None:
    async def scenario() -> None:
        examples = Path(__file__).resolve().parents[2] / "examples"
        app = create_app(
            Settings(
                max_in_flight_requests=4,
                policy_file=examples / "policies.json",
                text_rules_file=examples / "text-rules.json",
            )
        )
        entered = [asyncio.Event() for _ in range(4)]
        release = asyncio.Event()

        async def body(index: int) -> AsyncIterator[bytes]:
            entered[index].set()
            yield b'{"input":{"type":"text","text":"'
            await release.wait()
            yield b'private content"},"policy":"rules-only"}'

        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
                headers=auth_headers,
            ) as client,
        ):
            active = [
                asyncio.create_task(
                    client.post(
                        "/v1/moderations",
                        content=body(index),
                        headers={"Content-Type": "application/json"},
                    )
                )
                for index in range(4)
            ]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(event.wait() for event in entered)), 2
                )
                rejected = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            client.post("/v1/moderations", content=b"not consumed")
                            for _ in range(24)
                        )
                    ),
                    2,
                )
                for response in rejected:
                    assert response.status_code == 503
                    assert response.json()["error"]["code"] == "service_busy"
                    assert response.headers["Retry-After"] == "1"
                assert (await client.get("/health/ready")).status_code == 200
                text = (await client.get("/metrics")).text
                assert sample(text, "modera_moderation_requests_in_flight") == 4
                assert (
                    sample(
                        text, "modera_moderation_requests_total", {"status_code": "503"}
                    )
                    == 24
                )
                active[0].cancel()
                release.set()
                results = await asyncio.wait_for(
                    asyncio.gather(*active, return_exceptions=True), 2
                )
                assert isinstance(results[0], asyncio.CancelledError)
                for result in results[1:]:
                    assert not isinstance(result, BaseException)
                    assert result.status_code == 200
                recovered = await client.post(
                    "/v1/moderations",
                    json={
                        "input": {"type": "text", "text": "hello"},
                        "policy": "rules-only",
                    },
                )
                assert recovered.status_code == 200
                text = (await client.get("/metrics")).text
                assert sample(text, "modera_moderation_requests_in_flight") == 0
                assert (
                    sample(
                        text, "modera_moderation_requests_total", {"status_code": "200"}
                    )
                    == 4
                )
                assert (
                    sample(
                        text, "modera_moderation_requests_total", {"status_code": "0"}
                    )
                    == 1
                )
                assert (
                    sample(text, "modera_moderation_request_duration_seconds_count")
                    == 29
                )
                assert "private content" not in text
            finally:
                release.set()
                for task in active:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*active, return_exceptions=True)

    asyncio.run(scenario())
