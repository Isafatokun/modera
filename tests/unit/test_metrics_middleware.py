import asyncio

import pytest
from starlette.types import Message, Receive, Scope, Send

from modera.errors import RequestContextMiddleware
from modera.metrics import Metrics, MetricsMiddleware


@pytest.mark.parametrize("disconnect", [False, True])
def test_metrics_observe_sanitized_errors_and_failed_response_sends(
    disconnect: bool,
) -> None:
    measurements = Metrics()

    async def failing_app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("private error")

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        if disconnect:
            raise ConnectionError("connection closed")

    app = MetricsMiddleware(RequestContextMiddleware(failing_app), measurements)
    scope: Scope = {
        "type": "http",
        "path": "/v1/moderations",
        "method": "POST",
    }
    if disconnect:
        with pytest.raises(ConnectionError):
            asyncio.run(app(scope, receive, send))
    else:
        asyncio.run(app(scope, receive, send))
    assert (
        measurements.registry.get_sample_value(
            "modera_moderation_requests_total",
            {"status_code": "0" if disconnect else "500"},
        )
        == 1
    )
    assert (
        measurements.registry.get_sample_value("modera_moderation_requests_in_flight")
        == 0
    )
    assert (
        measurements.registry.get_sample_value(
            "modera_moderation_request_duration_seconds_count"
        )
        == 1
    )
