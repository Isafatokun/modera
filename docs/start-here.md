# Start here

Choose what you want to try. Both paths bind the API to your own machine.

| Goal | Start with | Requirements |
| --- | --- | --- |
| Learn requests and decisions | [Text rules demo](../README.md#run) | Python 3.12 and uv; no model download |
| Run real text and photo models | [Docker Compose setup](../deploy/reference/README.md) | Docker Compose v2; about 0.6 GiB of weights downloaded on first run |
| Connect your backend | [Go example](../clients/go/README.md) or [API reference](api.md) | A running service and its bearer key |

The rules demo checks example phrases only. It does not demonstrate model quality
or photo moderation. The real-model setup uses explicit, pinned example models
and thresholds; these are not a recommendation for your content or audience.
Neither path establishes production capacity or moderation accuracy.

## First real-model run

From a checkout of this repository, with Docker running:

```sh
export MODERA_API_KEY="$(openssl rand -hex 32)"
docker compose up --build --force-recreate --wait --wait-timeout 600
docker compose --profile checks run --rm --no-deps check
```

Keep the key in your backend or secret manager. The preparation container downloads
and warms the models; the service then reads the cached weights without downloading
at startup. Compose manages the cache volume and its permissions for you.

The check sends neutral text and a generated white image. It verifies that the API
works, not that harmful content will be detected. Stop with `docker compose down`;
this retains the cached weights for the next run.

Upload a photo from your machine using the same shell:

```sh
curl http://127.0.0.1:8000/v1/moderations \
  -H "Authorization: Bearer $MODERA_API_KEY" \
  -F 'file=@./photo.jpg'
```

Replace `./photo.jpg` with your own file. File uploads need no URL allowlist.
For remote photo URLs, explicitly allow the source host using the
[URL setup instructions](../deploy/reference/README.md#url-and-unavailable-source-checks).

## Understand the response

- `allow`: no configured rule reached a review or block threshold.
- `review`: your application should hold the content for human review.
- `block`: a configured block threshold was reached.
- An error: evaluation did not produce a decision. Do not treat it as an allow.

Modera returns evidence; your application takes the action. For unexpected results,
check `findings`, `coverage`, `policy`, `models` and `fallback` together. A low score
explains how the configured policy ran; it does not prove the content is safe.
See the [request diagram and concepts](how-it-works.md).

## If you get stuck

| Symptom | What to check |
| --- | --- |
| Service never becomes ready | Run `docker compose logs prepare modera`; check download access, free disk space and the reported configuration error. Redact secrets before sharing logs. |
| Port 8000 is occupied | Set `MODERA_PORT=8001`, restart Compose, and use that port in requests. |
| `401` | Use the key from the shell that started the service. Never send a key in a URL. |
| `501` | That input type has no configured backend. The rules demo supports text only. |
| Photo URL fails | Check the exact allowed hostname and HTTPS source. Try a file upload to separate retrieval from inference. |
| `503` | Read the error code and `Retry-After`; use bounded retries. Persistent failures need investigation. |
| `500` | An internal check failed. Keep the request ID for diagnosis; do not automatically retry. |

## Before exposing the service

Choose and evaluate models and policies for your actual inputs. Configure HTTPS,
network access, secret management and monitoring. Measure capacity on your hardware,
and test overload, restart and shutdown behaviour. Follow the
[operations guide](operations.md) and [reference setup notes](../deploy/reference/README.md)
for the deployment details.

## Generate your own rules demo

From the repository root:

```sh
uv sync --locked
uv run --locked python -m modera.cli init ./moderation-demo
cd moderation-demo
uv run --project .. --locked python -m modera.cli doctor --env-file .env
uv run --project .. --locked uvicorn modera.main:create_app --factory --env-file .env --host 127.0.0.1
```

`init` creates editable `policies.json`, `text-rules.json` and a private `.env`
containing a generated key. It refuses to overwrite an existing directory.
The example policy is named `rules-example`. No model or photo checks are installed.
Keep this demo on localhost; do not commit or share `.env`.

`doctor` checks configuration and required category mappings without loading models
or contacting the API. It does not certify readiness, resource capacity or accuracy.
Run it from the generated directory. Existing environment variables take priority
over `.env`; use a fresh shell if settings from another setup are still exported.
Uvicorn loads `.env` only because the command explicitly supplies `--env-file`.
