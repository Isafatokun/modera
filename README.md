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

For real text and photo models, see [Models](docs/models.md#set-up-classifiers).
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
- Ships a [Go client](clients/go/README.md) and a tested
  [reference upload gate](examples/upload_gate.py) showing how an application keeps
  an upload private until a completed decision arrives.

Modera does not publish content, keep a review queue or store results. Your
application owns those, including retrying safely: a retry is a new evaluation.

## Why not a hosted API?

Hosted moderation services already detect a lot. [OpenAI's moderation endpoint](https://developers.openai.com/api/docs/guides/moderation)
returns per-category flags and scores, [AWS Rekognition](https://docs.aws.amazon.com/rekognition/latest/APIReference/API_DetectModerationLabels.html)
returns labels above a minimum confidence you set, [Google SafeSearch](https://docs.cloud.google.com/vision/docs/detecting-safe-search)
returns likelihood levels, and [Azure AI Content Safety](https://learn.microsoft.com/en-us/azure/ai-services/content-safety/how-to/use-blocklist)
lets you add your own text blocklists. In each case your content is sent to the provider.
OpenAI's guide tells you to treat its scores as signals for your own policy and to expect
to recalibrate them, and AWS's tells you to use the returned labels to decide which
content is appropriate.

Modera is for when you want that decision to be a versioned, testable policy that sits
next to the models, with content that stays in your deployment. The price is that you run
the models, and the accuracy is yours to measure. The reference models are not good enough
on their own; see [accuracy](docs/accuracy.md).

## Documentation

| | |
| --- | --- |
| [API](docs/api.md) | Requests, results, errors, and how to act on them |
| [Configuration](docs/configuration.md) | Every setting |
| [Policies](docs/policies.md) | Thresholds and required checks |
| [Models](docs/models.md) | Choosing, preparing and replacing models |
| [Photo models](docs/image-models.md) | Detectors, ONNX and combining several photo models |
| [Operations](docs/operations.md) | Deployment, metrics, key rotation and overload |
| [Accuracy](docs/accuracy.md) | Measured text accuracy, calibration and review load |

## How it's tested

- Over 500 tests run on every change, calling the API in process: policies, limits, error
  classification, queues, overload and shutdown, model loading with small generated
  models, and the hardened container.
- The Go client has unit tests with the race detector. It has no live check against the
  API.
- The reference upload gate is tested against a real Modera process with a stalled
  model.
- [Accuracy](docs/accuracy.md) was measured separately, on public labelled text. The
  harness that produced it was removed to keep this repository small and is kept at
  the `evaluation-archive` git tag.

Not covered: photo accuracy, real-model serving speed, your platform's load balancer and termination, and
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
