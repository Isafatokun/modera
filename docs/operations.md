# Operations

Run one worker per instance. Each process loads its own models and keeps its own limits
and metrics. Scale by adding instances and scrape each one directly, since metrics are not
aggregated.

## Metrics

`GET /metrics` returns Prometheus text and needs the same bearer key as the API. Keep it
behind your network controls.

| Metric | Meaning |
| --- | --- |
| `modera_moderation_requests_total{status_code}` | Moderation responses by status. `0` means the response never completed, for example a cancelled request. |
| `modera_moderation_request_duration_seconds` | Request latency, including body reading, downloads, inference and failures. |
| `modera_moderation_requests_in_flight` | Requests being handled, including ones being rejected. |
| `modera_decisions_total{input_type,policy,action,fallback}` | Completed evaluations by the policy actually used. |
| `modera_category_score{input_type,category,source}` | Histogram of category scores from completed evaluations. |
| `modera_worker_active{worker}` | Occupied worker slots, including native work that outlives its HTTP request. |
| `modera_worker_waiting{worker}` | Jobs waiting for that worker. |

Counters and latency cover only `POST /v1/moderations`. The `worker` label is
`text_inference`, `image_inference` or `image_preparation`. Labels never contain content,
URLs, model IDs, credentials or request IDs, and unknown policy names never become label
values. Keep the number of policies and categories small, since each adds histogram
series. Decision counters count completed evaluations, not published content, and errors
produce no samples.

Watch the `503` rate next to latency and in-flight requests. The error code tells you
whether a `503` came from request capacity, a busy worker or an unavailable check.

## Overload

`MODERA_MAX_IN_FLIGHT_REQUESTS` (default 8) limits concurrent requests per process. After
authentication, extra requests get `503 service_busy` with `Retry-After: 1` before their
body is read.

Photo preparation and each photo model have one active slot and a FIFO queue of
`MODERA_IMAGE_QUEUE_SIZE` jobs (default 5), each waiting at most
`MODERA_IMAGE_QUEUE_WAIT_SECONDS` (default 2). A full queue returns `503 service_busy`,
and an expired wait returns `503 queue_wait_timeout`. Text inference and downloads have no
queue and reject immediately when busy. Text and each photo model are independent, so a
busy photo model does not block text, but CPU and memory are shared. Health checks and
scrapes bypass these limits.

The inference deadline (`MODERA_INFERENCE_TIMEOUT_SECONDS`, default 5) includes the time a
request waits in the queue. When a caller times out, its HTTP slot is released but the
native work keeps its worker slot until it finishes, so retries cannot pile hidden work on
top of work that is still running. Callers should retry with a deadline, a bounded number
of attempts and jitter.

Queues are in memory and per process. They absorb short bursts, not sustained overload,
and nothing survives a restart. Put connection limits and TLS at your ingress, because
Modera does not bound connections or the bytes a proxy buffers.

## Startup and shutdown

Models load and warm up before the service reports ready. Missing or incompatible weights
fail startup, unless text fallback applies. Route traffic on `/health/ready`, use
`/health/live` for liveness, and allow enough startup time for your models.

The container runs a single worker. On `SIGTERM`, Uvicorn stops accepting connections and
drains accepted requests for up to 30 seconds. Set the platform's termination grace period
above that, for example 45 seconds, and take the instance out of routing first. A native
inference call cannot be cancelled, so the platform has to enforce the final deadline. An
interrupted request has no saved result and should be retried on another instance.

Integration tests cover the process lifecycle with tiny models, so they verify behaviour,
not moderation quality or throughput:

```sh
uv run --locked --extra inference pytest tests/integration/test_service_process.py -q
```

Measure throughput on your own hardware before sizing a deployment. Ingress behaviour and
termination need testing on your platform.

## Rotate the API key

1. Deploy the new key as `MODERA_API_KEY` and keep the old one as
   `MODERA_PREVIOUS_API_KEY`. Wait until every instance accepts both.
2. Move callers and the metrics scraper to the new key and confirm they work.
3. Remove `MODERA_PREVIOUS_API_KEY`, roll it out, and confirm the old key is rejected.

Both keys have the same permissions. There is no expiry or live reload, so each change is a
restart. Keep the overlap short, and never keep a compromised key for convenience.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Never becomes ready | The service log and the `prepare_models` output: download access, disk space, and the configuration error. Redact secrets before sharing. |
| Port 8000 is taken | Start uvicorn with `--port 8001` and use that port. |
| `401` | Use the key from the shell that started the service. Never put a key in a URL. |
| `501` | That input type has no backend. The rules demo handles text only. |
| Photo URL fails | Check the exact allowed hostname and that the source is HTTPS. Try an upload to separate fetching from inference. |
| `503` | Read the error code and `Retry-After`, and retry a bounded number of times. Persistent failures need investigation. |
| `500` | An internal check failed. Keep the request ID and don't retry automatically. `model_not_loaded` needs a restart. |
