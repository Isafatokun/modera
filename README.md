# Modera

[![CI](https://github.com/Isafatokun/modera/actions/workflows/ci.yml/badge.svg)](https://github.com/Isafatokun/modera/actions/workflows/ci.yml)

A self-hosted moderation API for text and photos.

You send one item and name a policy. Modera runs the checks that policy needs and
returns `allow`, `review` or `block`, along with the scores and settings behind the
decision. The models detect, the policy decides, and your application acts. Two
products can use the same models and treat the findings differently.

Modera is experimental, and its accuracy is modest. Measured on two public labelled
text datasets, no reference model reaches a 90% recall / 5% false-flag target: the best
catch 32% (French) and 48% (English) of toxic text at about 5% false flags. See
[accuracy](docs/accuracy.md) for the method, the numbers and the limits. Photo accuracy
has not been measured.

## Quick start

You need Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
This demo matches literal phrases, so it downloads no model.

```sh
uv sync --locked
export MODERA_API_KEY="$(openssl rand -hex 32)"
MODERA_POLICY_FILE=examples/policies.json \
MODERA_TEXT_RULES_FILE=examples/text-rules.json \
uv run --locked uvicorn modera.main:create_app --factory
```

In another shell with the same key:

```sh
curl http://127.0.0.1:8000/v1/moderations \
  -H "Authorization: Bearer $MODERA_API_KEY" -H 'Content-Type: application/json' \
  -d '{"input":{"type":"text","text":"example prohibited phrase"},"policy":"rules-only"}'
```

```json
{
  "request_id": "req_edca92b36b904b4a90c73edbc4b525e8",
  "input_type": "text",
  "policy": {"id": "rules-only", "version": "1"},
  "decision": {"action": "block", "reasons": ["rules.prohibited_terms"]},
  "findings": [{"category": "rules.prohibited_terms", "score": 1.0, "source": "rules"}],
  "coverage": {"evaluated": ["rules.prohibited_terms"], "unsupported": [], "not_applicable": []},
  "models": [],
  "rules_version": "1",
  "fallback": false,
  "preprocessing_version": "text-v1.5362714a..."
}
```

For real text and photo models, use the [Docker Compose setup](deploy/reference/README.md).
[Start here](docs/start-here.md) covers both paths and common problems.

## What it does

- Takes text, a photo upload, or an HTTPS photo URL from hosts you allow.
- Applies named, versioned [policies](docs/policies.md): per-category thresholds,
  required checks, and rules that combine several models.
- Reports what it evaluated. A missing check is never returned as an `allow`, and an
  evaluation that cannot finish returns an error, not a decision.
- Runs local classifiers and detectors on CPU. Models are pinned to a commit and
  loaded offline.
- Exposes health checks and Prometheus metrics, bounds its queues, and drains
  requests on shutdown.
- Ships a [Go client](clients/go/README.md) and a runnable
  [example application](clients/go/cmd/moderate/README.md).

Modera does not publish content, keep a review queue or store results. Your
application owns those, including retrying safely: a retry is a new evaluation.

## Documentation

| | |
| --- | --- |
| [How it works](docs/how-it-works.md) | Request flow and how to use a result |
| [API](docs/api.md) | Requests, responses and errors |
| [Configuration](docs/configuration.md) | Every setting |
| [Policies](docs/policies.md) | Thresholds and required checks |
| [Models](docs/models.md) | Choosing, preparing and replacing models |
| [Photo models](docs/image-models.md) | Detectors, ONNX and combining several photo models |
| [Operations](docs/operations.md) | Deployment, metrics, key rotation and overload |
| [Accuracy](docs/accuracy.md) | Measured text accuracy, calibration and review load |
| [Evaluation](evaluation/README.md) | Reproducing the text numbers, and checking a policy on your own photos |

## How it's tested

- Over 500 tests run on every change, calling the API in process: policies, limits, error
  classification, queues, overload and shutdown, model loading with small generated
  models, and the hardened container.
- The Go client has unit tests with the race detector and a live check against the real
  API, and CI installs it into a fresh module.
- A Compose smoke test starts the reference setup from an empty cache with the real
  checkpoints.
- [Accuracy](docs/accuracy.md) is measured separately, on public labelled text.

Not covered: photo accuracy, your platform's load balancer and termination, and
production capacity. Nothing here measures throughput.

## Development

```sh
uv sync --locked
uv run --locked ruff check . && uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
```

Source is in `src/modera`, tests in `tests/`. See [CONTRIBUTING](CONTRIBUTING.md) for the
full checks and [SECURITY](SECURITY.md) to report a vulnerability.

## Licence

Apache-2.0, see [LICENSE](LICENSE). Model weights are separate and have their own
terms; check them before you deploy a model.
