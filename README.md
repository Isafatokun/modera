# Modera

Text and photo moderation service.

Modera is being built incrementally as a usable service and a system design study.
This first stage provides the application foundation: configuration, health checks,
packaging, a container definition, and automated quality checks. Moderation endpoints
and models are not implemented yet.

## Run locally

Install Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
uv run --locked uvicorn modera.main:create_app --factory --reload
curl --fail http://127.0.0.1:8000/health/ready
```

The server binds to localhost by default. `--reload` is for development only.

## Configuration

Settings are validated when the application is created. Invalid values stop startup.
Set environment variables in your shell or deployment environment; `.env` files are
not loaded automatically. `.env.example` lists the supported variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODERA_DOCS_ENABLED` | `false` | Enable `/docs` and `/openapi.json` |

Debug mode is disabled. Health responses contain no configuration or dependency details.

## Health checks

| Endpoint | Purpose |
| --- | --- |
| `GET /health/live` | Confirm the process can serve an HTTP request |
| `GET /health/ready` | Return `200` after startup and `503` outside the active lifespan |

Successful checks return `{"status":"ok"}`. There are no model dependencies at this
stage, so readiness does not yet verify model availability. Health checks are unauthenticated.

## Development

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build
```

Application code lives in `src/modera/`. Unit tests cover settings; integration tests
exercise the ASGI application, configuration isolation, and startup/shutdown behavior.
`pyproject.toml` declares dependencies and tool settings. Commit `uv.lock` whenever
dependencies change; installation with `--locked` rejects an outdated lockfile.

## Container

```sh
docker build -t modera:local .
docker run --rm --init --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --tmpfs /tmp \
  -p 127.0.0.1:8000:8000 modera:local
```

The image uses a non-root user and installs only runtime dependencies. It runs one
Uvicorn process, with access logging and forwarded-header trust disabled. Termination
signals reach Uvicorn for graceful shutdown. Configure TLS at your deployment ingress.
Use `/health/ready` for traffic routing and `/health/live` for process health.

## Next stages

1. Define the moderation API, policy configuration, and error contract.
2. Add authentication, request limits, and secure media handling.
3. Integrate text and photo models with bounded inference capacity and deadlines.
4. Add metrics, dependency-failure tests, and deployment validation.

Future extensions include a client SDK with typed results and bounded retries, and
durable asynchronous moderation with persistent input storage, a queue, job status,
idempotency, and recovery after worker restarts.

Production deployment requires those service controls and validation with the real
models. The current foundation does not accept or moderate user content.
