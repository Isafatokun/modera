# Modera

Text and photo moderation service.

A synchronous API with replaceable text/photo classifiers, configurable policies,
optional text rules, and explicit failure handling. Models and policies are operator
configured; an unconfigured modality returns `501`.

## Run

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
uv sync --locked
export MODERA_API_KEY="$(uv run --locked python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run --locked uvicorn modera.main:create_app --factory --reload
```

The server binds to localhost. Use `--reload` only for development.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODERA_API_KEY` | Required | Shared bearer API key; 32–512 printable ASCII characters, no spaces |
| `MODERA_MAX_IN_FLIGHT_REQUESTS` | `8` | Maximum active moderation requests per process; no waiting queue |
| `MODERA_DOCS_ENABLED` | `false` | Enable `/docs` and `/openapi.json` |
| `MODERA_POLICY_FILE` | Unset | Path to a validated policy JSON file |
| `MODERA_TEXT_MODEL__ID` / `__REVISION` | Unset | Text model ID and full commit SHA |
| `MODERA_IMAGE_MODEL__ID` / `__REVISION` | Unset | Photo model ID and full commit SHA |
| `MODERA_IMAGE_ALLOWED_HOSTS` | `[]` | JSON array of exact DNS hostnames allowed for image downloads |

Set variables through the shell or your deployment's secret manager. `.env` files
are not loaded automatically. Missing/invalid keys and invalid policy files prevent
startup. Generate a random key for each deployment; never commit it or embed it in
browser/mobile code. Use HTTPS at the deployment ingress. Rotation requires replacing
the secret and restarting instances and callers that use it.

Model selections default to unset. Configure repository ID, pinned revision and label
mapping; see [models, replacement, and rules fallback](docs/models.md). Classifier
weights must be prepared before startup. Rules-only deployments need no ML packages.

## API

`POST /v1/moderations` requires `Authorization: Bearer <key>`. It accepts JSON text,
a JSON image URL, or a multipart image file. See [requests, results, and errors](docs/api.md).

[Policies](docs/policies.md) control category thresholds and required checks. No
thresholds are installed by default. URL retrieval requires an explicit host allowlist.

Health checks need no credentials: `/health/live` checks HTTP availability;
`/health/ready` checks readiness for the configured model/rules mode. Successful checks return
`{"status":"ok"}`. Every application HTTP response includes `X-Request-ID`.

## Container

```sh
docker build -t modera:local .
docker run --rm --init --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --tmpfs /tmp -e MODERA_API_KEY \
  -p 127.0.0.1:8000:8000 modera:local
```

Runs as a non-root user with runtime dependencies only. Access logging and proxy
header trust are disabled. Mount policy files read-only when configured.

## Development

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build
```

Code: `src/modera/`. Tests: `tests/unit/`, `tests/integration/`.
Dependencies: `pyproject.toml` and `uv.lock`.

## Roadmap

Operational metrics, additional inference adapters, a client SDK with bounded retries,
and durable asynchronous moderation jobs.
