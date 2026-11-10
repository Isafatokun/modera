# Operations

Run one worker per instance. Each process loads its own models and keeps its own
limits and metrics. Scale by adding instances and scrape each one directly, since
metrics are not aggregated across processes.

## Metrics

`GET /metrics` returns Prometheus text and needs the same bearer key as the API. Keep
it behind your network controls.

| Metric | Meaning |
| --- | --- |
| `modera_moderation_requests_total{status_code}` | Moderation responses by status. `0` means the response never completed, for example a cancelled request. |
| `modera_moderation_request_duration_seconds` | Request latency, including body reading, downloads, inference and failures. |
| `modera_moderation_requests_in_flight` | Requests being handled, including ones being rejected. |
| `modera_decisions_total{input_type,policy,action,fallback}` | Completed evaluations by the policy actually used. |
| `modera_category_score{input_type,category,source}` | Histogram of category scores from completed evaluations. |
| `modera_worker_active{worker}` | Occupied worker slots, including native work that outlives its HTTP request. |
| `modera_worker_waiting{worker}` | Jobs waiting for that worker. |

Counters and latency cover only `POST /v1/moderations`; health checks and scrapes are
excluded. The `worker` label is one of `text_inference`, `image_inference` or
`image_preparation`. Labels never contain content, URLs, model IDs, credentials or
request IDs, and unknown policy names in requests never become label values. Keep the
number of policies and categories small, since each adds histogram series.

Watch the `503` rate next to latency and in-flight requests. The error code tells
callers whether a `503` came from request capacity, a busy worker or an unavailable
check. Decision counters count completed evaluations, not published content, and
errors produce no samples. Scores are model signals, so read their distributions
with the model, policy and input mix in mind.

## Overload

`MODERA_MAX_IN_FLIGHT_REQUESTS` (default 8) limits concurrent requests per process.
After authentication, extra requests get `503 service_busy` with `Retry-After: 1`
before their body is read.

Photo preparation and each photo model have one active slot and a FIFO queue of
`MODERA_IMAGE_QUEUE_SIZE` jobs (default 5), each waiting at most
`MODERA_IMAGE_QUEUE_WAIT_SECONDS` (default 2). A full queue returns
`503 service_busy`, and an expired wait returns `503 queue_wait_timeout`. Text
inference and downloads have no queue and reject immediately when busy. Text and each
photo model are independent, so a busy photo model does not block text. CPU and
memory remain shared. Health checks and metric scrapes bypass these limits.

When a caller times out, its HTTP slot is released but the native work keeps its worker
slot until it finishes, so retries cannot pile hidden work on top of work that is
still running. Callers should retry with a deadline, a bounded number of attempts and
jitter. Queues are in memory and per process: they absorb short bursts, not sustained
overload, and nothing survives a restart. Put connection limits and TLS at your
ingress, because Modera does not bound connections or the bytes a proxy buffers.

## Startup and shutdown

Models load and warm up before the service reports ready. Missing or incompatible
weights fail startup, unless text fallback applies. Route traffic on `/health/ready`
and use `/health/live` for liveness, and allow enough startup time for your models.

The container runs a single worker. On `SIGTERM`, Uvicorn stops accepting connections
and drains accepted requests, for up to 30 seconds. Set the platform's termination
grace period above that, for example 45 seconds, and take the instance out of routing
first. A native inference call cannot be cancelled, so the platform has to enforce the
final deadline. An interrupted request has no saved result and should be retried on
another instance.

The [rehearsal](../e2e/rehearsal/README.md) checks this with the Go client, two
real-model processes and NGINX, and CI runs it. These integration tests cover the
process lifecycle:

```sh
uv run --locked --extra inference pytest tests/integration/test_service_process.py -q
```

They use tiny test models, so they verify behaviour, not moderation quality or
throughput. For throughput, run the [load test](../loadtests/README.md). Ingress
behaviour and termination still need testing on your hosting platform.

## Rotate the API key

To rotate without switching every caller at once:

1. Deploy the new key as `MODERA_API_KEY` and keep the old one as
   `MODERA_PREVIOUS_API_KEY`. Wait until every instance accepts both.
2. Move callers and the metrics scraper to the new key and confirm they work.
3. Remove `MODERA_PREVIOUS_API_KEY`, roll it out, and confirm the old key is rejected.

Both keys have the same permissions. Keep the overlap short: there is no expiry or
live reload, so each change is a restart. Never keep a compromised key for convenience.
