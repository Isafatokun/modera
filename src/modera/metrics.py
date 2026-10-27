from collections.abc import Callable
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, Depends, Request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from modera.auth import authenticate
from modera.workers import WorkerCapacity

router = APIRouter(dependencies=[Depends(authenticate)])


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "modera_moderation_requests_total",
            "Moderation requests by HTTP status; 0 means no complete response.",
            ["status_code"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "modera_moderation_request_duration_seconds",
            "Moderation HTTP request duration, including body reading and failures.",
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60),
            registry=self.registry,
        )
        self.active = Gauge(
            "modera_moderation_requests_in_flight",
            "Moderation HTTP requests currently being handled, including rejections.",
            registry=self.registry,
        )
        self.worker_active = Gauge(
            "modera_worker_active",
            "Occupied native worker slots, including work after HTTP cancellation.",
            ["worker"],
            registry=self.registry,
        )
        self.worker_waiting = Gauge(
            "modera_worker_waiting",
            "Jobs waiting for a native worker slot.",
            ["worker"],
            registry=self.registry,
        )

    def observe_worker(
        self,
        worker: Literal["text_inference", "image_inference", "image_preparation"],
        capacity: Callable[[], WorkerCapacity],
    ) -> None:
        self.worker_active.labels(worker).set_function(lambda: capacity().active)
        self.worker_waiting.labels(worker).set_function(lambda: capacity().waiting)


class MetricsMiddleware:
    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["path"] != "/v1/moderations"
            or scope["method"] != "POST"
        ):
            await self.app(scope, receive, send)
            return
        started = perf_counter()
        status = 0
        completed = False
        self.metrics.active.inc()

        async def observe_response(message: Message) -> None:
            nonlocal status, completed
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                completed = True

        try:
            await self.app(scope, receive, observe_response)
        finally:
            self.metrics.requests.labels(str(status if completed else 0)).inc()
            self.metrics.duration.observe(perf_counter() - started)
            self.metrics.active.dec()


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    measurements: Metrics = request.app.state.metrics
    return Response(
        generate_latest(measurements.registry),
        headers={"Content-Type": CONTENT_TYPE_LATEST, "Cache-Control": "no-store"},
    )
