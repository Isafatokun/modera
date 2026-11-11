# How we know it works

Each claim below is tied to the check that supports it, where that check runs, and what
it does not show. Nothing here measures moderation accuracy.

| Claim | Check | Runs | Does not show |
| --- | --- | --- | --- |
| Requests, policies, errors and limits behave as documented | `tests/` (over 500 tests that call the API in process through FastAPI's test client) | Every change, in the CI `checks` job | Anything about model quality |
| Models load and fail safely | `pytest -m inference` and `pytest -m onnx`, using tiny generated models | CI `inference` and `onnx` jobs | That a real model is accurate |
| The container is hardened and works | Read-only, no capabilities, authentication, image codecs, rules-only, and a restart: `.github/scripts/` and `tests/integration/test_container.py`, `test_compose.py` | CI `container` and `inference` jobs | Behaviour on your platform |
| Text moderation accuracy on public labelled data | `python -m evaluation.text`: six pinned models, two datasets, cutoffs chosen on a validation half and scored on a held-out half, with calibration and review-load analysis. Results are in [accuracy](accuracy.md) | On request. The raw scores are committed, and the report is rebuilt from them with `report` | Your content, photos, or anything about prevalence in production. The reference models do not meet the 90% / 5% target |
| The Go client matches the API | Unit tests with `-race`, `staticcheck`, the live API run in `clients/go/integration.py`, a fresh-module install, and a Go 1.23 build | CI `go-client` and `go-minimum` jobs | Your application's own error handling |
| The Compose setup works from an empty cache with the real checkpoints | `e2e/reference/verify.py` | CI `Reference profile` workflow, on a change to that script or by hand | Accuracy, or throughput |
| Overload, retry, drain, restart and forced termination behave | `e2e/rehearsal/rehearse.py`: the Go client, NGINX and two real-model processes | CI `Deployment rehearsal` workflow, on changes to the service, the client or the rehearsal, or by hand | Your platform's ingress and termination, or capacity |

## Run them yourself

```sh
uv run --locked ruff check . && uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest                                   # fast checks, no models
uv sync --locked --extra inference --extra onnx
uv run --locked pytest -m "inference or onnx"            # real model loading
```

The checks that need Docker, Linux or real model downloads are in `e2e/`. Their
READMEs say how to run them, and the rehearsal needs Linux and NGINX.

## Measured on request, not on every change

These need your hardware or your data, so they are tools and not tests.

- [`loadtests/`](../loadtests/README.md) measures latency and capacity with the real
  models. CI only checks that its scripts start. Run it before you size a deployment.
- [`evaluation/`](../evaluation/README.md) scores a policy against your own labelled
  photos and reports misses, false flags and failed checks. `python -m evaluation.text`
  does the same for text on the public datasets. Run these before a policy acts on real
  content.

## What the repository does not establish

- **Moderation accuracy for your content.** Text accuracy is measured on two public
  datasets and the 90% recall / 5% false-flag target is not met (see
  [accuracy](accuracy.md)). Photo accuracy is not measured. Public benchmarks are not
  your traffic, and prevalence in production changes how many flags are false alarms.
- **Behaviour on your platform.** The rehearsal covers the process and a gateway on a
  Linux runner, and does not cover your load balancer, TLS or termination controller.
- **Production capacity.** The load test is a starting point, run on one machine.
