# Modera

Text and photo moderation service.

Under development. Health checks and moderation request validation are available.
Model inference is not implemented; valid moderation requests return `501` without a decision.

## Run locally

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
uv sync --locked
uv run --locked uvicorn modera.main:create_app --factory --reload
curl --fail http://127.0.0.1:8000/health/ready
```

The server binds to localhost by default. `--reload` is for development only.

## API

`POST /v1/moderations` accepts one text or image input:

- `application/json`: text or an HTTPS image URL.
- `multipart/form-data`: an image `file` and optional `policy`.

See the [API reference](docs/api.md) for request formats and limits. Authentication,
image decoding, policy resolution, and secure URL retrieval are not implemented yet.
The current endpoint validates requests only.

Errors share a stable JSON envelope with an error code, optional field name, retry
guidance, and request ID. Every application HTTP response includes `X-Request-ID`.

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

Successful checks return `{"status":"ok"}`. Health checks are unauthenticated.
Readiness currently checks application startup only; models are not integrated.

## Development

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build
```

Application code: `src/modera/`. Tests: `tests/unit/` and `tests/integration/`.
Dependencies and tool settings: `pyproject.toml`. Resolved versions: `uv.lock`.

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

## Roadmap

- Text and photo moderation with configurable policies.
- Authentication, input limits, and secure media retrieval.
- Bounded inference capacity, deadlines, and structured errors.
- Operational metrics and validation with real models.
- Client SDK with typed results and bounded retries.
- Durable asynchronous jobs with input storage, status, idempotency, and recovery.
