# Operations

Run one worker per instance. Each process loads its own model weights and maintains
its own concurrency limits and metrics. Scale with separate instances when memory
and CPU budgets permit; scrape each instance directly, rather than a load-balanced
metrics URL. Multiprocess metric aggregation is not configured.

## Metrics

`GET /metrics` requires `Authorization: Bearer <key>` and returns Prometheus text
with `Cache-Control: no-store`. Configure the scraper's bearer credential through
its secret store. Keep the endpoint behind the deployment's network controls.

| Metric | Meaning |
| --- | --- |
| `modera_moderation_requests_total{status_code}` | Completed moderation HTTP responses by status; `0` means the response did not complete, such as cancellation |
| `modera_moderation_request_duration_seconds` | Histogram covering body reading, validation, downloads, inference, and response sending, including failures |
| `modera_moderation_requests_in_flight` | Active moderation HTTP requests, including requests being rejected |

Only `POST /v1/moderations` is measured. Scrapes, health checks, and other paths are
excluded. Labels contain no content, URLs, policy/model IDs, credentials, or request
IDs. Counters reset on restart; the monitoring system retains history. HTTP activity
does not measure native inference still running after an HTTP timeout/cancellation.

Track the `503` rate alongside latency and active requests. A `503` can indicate
request capacity, an occupied inference/validation worker, or an unavailable check;
the API error code distinguishes these cases for callers. Size and tune limits
using the chosen models, instance resources, media sizes, and representative traffic.

## Overload and recovery

`MODERA_MAX_IN_FLIGHT_REQUESTS` defaults to 8 per process. After authentication,
excess requests receive `503 service_busy` with `Retry-After: 1` before their body
is read. Photo validation/preparation and inference each have one active slot and
a bounded FIFO: `MODERA_IMAGE_QUEUE_SIZE=5`, with at most
`MODERA_IMAGE_QUEUE_WAIT_SECONDS=2` seconds of waiting inside the stage deadline.
Set queue size to `0` for immediate rejection. A full queue returns `503 service_busy`;
an expired wait returns `503 queue_wait_timeout`. Both include `Retry-After: 1`.
Downloads and text inference remain fail-fast. Health checks and scrapes bypass
these limits.
Text and photo inference each have one slot and reuse their loaded model. A busy
photo model does not occupy the text inference slot, or vice versa. This separates
inference admission; HTTP admission, CPU, memory, and the interpreter remain shared.

Caller retries should have a deadline, bounded attempts, and jitter. A caller
timeout or cancellation releases its HTTP slot, but native work keeps its worker
slot until completion. This prevents retries from accumulating hidden native work.
Put connection/request limits and TLS at the ingress as well: application admission
does not bound connections or bytes buffered by the HTTP server or proxy.

Waiting jobs are process-local and are not submitted to a hidden executor queue.
Worker cleanup rejects remaining waiting/new work; already running native work may
finish. Nothing is durable across restart. Bounded waiting absorbs short bursts,
not sustained overload; it does not coordinate separate instances or increase
model throughput.

Integration tests exercise concurrent request saturation, rejection, cancellation,
and recovery. They verify behavior, not deployment throughput or model capacity.
For real-model HTTP measurements, run the [capacity benchmark](../benchmarks/README.md).

## Startup and shutdown

Model loading and warmup finish before readiness and HTTP serving. Missing or
incompatible weights fail startup, unless the configured text fallback applies.
Use `/health/ready` for traffic routing and `/health/live` for liveness; allow
enough startup time for the selected models on the deployment hardware.

The container explicitly runs one worker, even if `WEB_CONCURRENCY` is set.
On `SIGTERM`, Uvicorn stops accepting connections and drains accepted requests
before application cleanup. Its request-drain timeout is 30 seconds. Configure
the platform's termination grace period above that, for example 45 seconds, and
remove the instance from ingress routing before termination. Native inference
cannot be forcibly cancelled by this timeout; the platform must enforce the final
process termination deadline. An interrupted request has no saved result and may
need retrying against another instance.

Reproduce the process checks with:

```sh
uv run --locked --extra inference pytest tests/integration/test_service_process.py -q
```

These cover authenticated HTTP requests, real local classifier loading, draining
an active photo request, `SIGTERM`, restart and missing-weight startup failure.
The tiny test classifier verifies the lifecycle, not moderation quality. Container
builds, ingress behavior and platform termination still need deployment testing.
