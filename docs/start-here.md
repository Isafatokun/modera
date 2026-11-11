# Start here

Pick a path. Both bind to localhost.

| Goal | Start with | Needs |
| --- | --- | --- |
| Try requests and decisions | [Quick start](../README.md#quick-start) | Python 3.12 and uv, no model download |
| Run real text and photo models | [Real models](#real-models) | Python 3.12, uv and about 0.6 GiB of weights on first run |
| Connect your backend | [Go client](../clients/go/README.md) or [API](api.md) | A running service and its key |

The rules demo matches example phrases and shows nothing about model quality or photo
moderation. The example models are pinned, and the example thresholds are not a
recommendation for your content. Neither path measures capacity or accuracy.

## Real models

Install the CPU dependencies, prepare the pinned weights once, then start the service:

```sh
uv sync --locked --extra inference
uv run --locked --extra inference python -m modera.prepare_models --config examples/models.json
export MODERA_API_KEY="$(openssl rand -hex 32)"
export MODERA_MODEL_FILE=examples/models.json
export MODERA_POLICY_FILE=examples/policies.json
uv run --locked --extra inference uvicorn modera.main:create_app --factory
```

Preparation downloads and warms the models, and the service then reads the cached weights
without touching the network. The example models and thresholds are placeholders; see
[Models](models.md#set-up-classifiers). Upload a photo with the same shell:

```sh
curl http://127.0.0.1:8000/v1/moderations \
  -H "Authorization: Bearer $MODERA_API_KEY" \
  -F 'file=@./photo.jpg'
```

Uploads need no URL allowlist. To fetch remote photos, allow the host with
`MODERA_IMAGE_ALLOWED_HOSTS`, as described in the [API](api.md). A container build is
described in [Models](models.md) and [Operations](operations.md).

## Reading the response

- `allow`: no configured check reached a threshold.
- `review`: hold the content for human review.
- `block`: a block threshold was reached.
- an error: no decision was made, so never treat it as an `allow`.

Modera returns evidence and your application takes the action. If a result surprises
you, read `findings`, `coverage`, `policy`, `models` and `fallback` together; a low score
only shows how the configured policy ran. See [using the result](api.md#using-the-result).

## When it doesn't work

| Symptom | Check |
| --- | --- |
| Never becomes ready | The service log and the `prepare_models` output: download access, disk space, and the configuration error (redact secrets before sharing). |
| Port 8000 is taken | Start uvicorn with `--port 8001` and use that port. |
| `401` | Use the key from the shell that started the service, and never put a key in a URL. |
| `501` | That input type has no backend. The rules demo handles text only. |
| Photo URL fails | Check the exact allowed hostname and that the source is HTTPS. Try an upload to separate fetching from inference. |
| `503` | Read the error code and `Retry-After`, and retry a bounded number of times. Persistent failures need investigation. |
| `500` | An internal check failed. Keep the request ID and don't retry automatically. |

## Before you expose it

Evaluate models and policies on your own inputs. Set up HTTPS, network access, secret
management and monitoring, measure capacity on your hardware, and test overload,
restart and shutdown. The [operations guide](operations.md) covers the details.

## Generate a rules demo of your own

```sh
uv sync --locked
uv run --locked python -m modera.cli init ./moderation-demo
cd moderation-demo
uv run --project .. --locked python -m modera.cli doctor --env-file .env
uv run --project .. --locked uvicorn modera.main:create_app --factory --env-file .env --host 127.0.0.1
```

`init` writes editable `policies.json` and `text-rules.json` and a private `.env` with a
generated key. It will not overwrite an existing directory, and the example policy is
named `rules-example`. Keep `.env` private and out of version control.

`doctor` checks the configuration and category mappings without loading models or
contacting the API. It does not check readiness, capacity or accuracy. Existing
environment variables take priority over `.env`, so use a fresh shell if another setup
is still exported.
