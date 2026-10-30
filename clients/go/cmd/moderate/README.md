# Runnable application example

This command shows an application consuming the Go SDK. It produces one JSON
object with an application disposition and the original moderation result (or
safe error metadata). It does not publish content, persist a review queue, or
perform any business action. Your application owns those actions and their
idempotency.

## Run the rules example

From the repository root, start the example API in one terminal. Set a fresh
`MODERA_API_KEY` in the environment of both terminals; do not put it in flags.

```sh
MODERA_POLICY_FILE=examples/policies.json \
MODERA_TEXT_RULES_FILE=examples/text-rules.json \
uv run --locked uvicorn modera.main:create_app --factory --host 127.0.0.1 --port 8000
```

In the other terminal, build the binary and submit content on stdin:

```sh
cd clients/go
go build -o /tmp/modera-moderate ./cmd/moderate
printf 'Bonjour, merci.' | /tmp/modera-moderate --policy rules-only
printf 'example prohibited phrase' | /tmp/modera-moderate --policy rules-only
printf 'example prohibited phrase' | /tmp/modera-moderate --policy rules-review
```

These produce `publish`, `reject`, and `review`, respectively. The policies use
an artificial literal phrase to demonstrate integration; they are not evidence
of moderation accuracy. The API must be reachable before running the client.

## Photo inputs

```sh
/tmp/modera-moderate --input image < photo.png
printf '%s' 'https://your-allowed-media-host.example/photo.png' \
  | /tmp/modera-moderate --input image-url
```

The rules-only server has no image backend, so a valid photo produces `pending`
with a typed backend error. For real photo results, use the
[reference deployment](../../../../deploy/reference/README.md) with the same key,
then use its configured `default` policy. URL inputs also require an exact server
host allowlist; the CLI sends the URL to Modera without fetching it locally.
The placeholder hostname above must be replaced by your configured media host.

Text is preserved, including whitespace/newlines, within 4,096 Unicode characters.
Photo stdin is bounded at 8 MiB; URL stdin at 2,048 bytes, with surrounding
whitespace stripped. Input must finish before the network deadline starts.
Ctrl+C interrupts a blocked input read; during network work it cancels the request.
The default deadline is 10 seconds across up to three attempts. Override with
`--timeout 5s --attempts 1`. HTTPS is required outside loopback.

## Consume the outcome

| Exit | Disposition | Example application behavior |
| --- | --- | --- |
| `0` | `publish` | Configured checks allowed the input; application may publish it. |
| `2` | `review` | Hold content and create an application review case. |
| `3` | `reject` | Keep content unpublished and follow the application's rejection flow. |
| `4` | `pending` | No trustworthy decision; hold content and surface/retry the failure as appropriate. |
| `1` | No complete JSON outcome | Locally invalid flags/input/configuration or an output write failure. |

`--help` exits `0` with help on stderr and no JSON. Nonzero decision exits are
intentional: handle them explicitly in shells using `set -e`. Run the built binary
when checking exit codes; `go run` does not preserve the program's nonzero status.

A fallback `allow` goes to `review` in this application's conservative example
policy. A fallback `block` stays `reject`. The original action, fallback flag,
coverage, request ID and policy/model provenance remain visible in `result`.
These application dispositions do not extend the Modera API or alter SDK behavior.
An `allow` covers only the configured categories, not every possible safety concern.

Errors have `disposition: pending`, an `error` object and no `result`. For API
errors, metadata includes the validated code/status/request ID, attempts and
effective retryability. Authentication and permanent backend errors require
configuration/operator action; `pending` does not mean retry forever. Retries
perform new evaluations; deduplicate subsequent business actions yourself.

The command never prints submitted content, source URLs, credentials or untrusted
server error messages. Results include scores and provenance, so handle that
metadata according to your application's logging policy. Transport and protocol
failures never become publish decisions.

The normal Go race/static checks test this command. The live integration check
also runs the built binary against the actual API and verifies all four
dispositions plus uploaded-image and URL error paths:

```sh
# From the repository root, with Go on PATH:
uv run --locked python clients/go/integration.py
```
