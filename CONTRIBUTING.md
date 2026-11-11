# Contributing

Small, focused pull requests are easiest to review.

## Setup

You need Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/). Go
1.23 or later is needed only for `clients/go`.

```sh
uv sync --locked
```

## Checks

Run these before you open a pull request. CI runs the same ones.

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
```

Tests that load real models need the optional extras:

```sh
uv sync --locked --extra inference --extra onnx
uv run --locked pytest -m "inference or onnx"
```

For the Go client, run `go test -race ./...` and `go vet ./...` in `clients/go`.

## Expectations

- Add a test for a bug fix that fails without the fix.
- Keep functions short. Ruff enforces a complexity limit on `src/`.
- Update the docs when behaviour changes, and keep them to the point.
- Say what you ran, and what you could not run, in the pull request.
- Don't claim model accuracy without evidence from labelled data. Unit tests can't show it.

[AGENTS.md](AGENTS.md) describes the review passes maintainers apply before a change is
published. For a security problem, follow [SECURITY.md](SECURITY.md) instead of opening a
public issue.
