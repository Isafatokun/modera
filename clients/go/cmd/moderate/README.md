# Example application

A small command that uses the Go client the way an application would. It reads content
on stdin and prints one JSON object with an application disposition and either the
original moderation result or safe error details. It publishes nothing and keeps no
review queue. Your application owns those actions and must make them idempotent.

## Try it with the rules demo

Start the API in one terminal. Both terminals need the same fresh `MODERA_API_KEY`,
set in the environment and not as a flag.

```sh
MODERA_POLICY_FILE=examples/policies.json \
MODERA_TEXT_RULES_FILE=examples/text-rules.json \
uv run --locked uvicorn modera.main:create_app --factory --host 127.0.0.1 --port 8000
```

In the other, build the binary and send text:

```sh
cd clients/go
go build -o /tmp/modera-moderate ./cmd/moderate
printf 'Bonjour, merci.' | /tmp/modera-moderate --policy rules-only
printf 'example prohibited phrase' | /tmp/modera-moderate --policy rules-only
printf 'example prohibited phrase' | /tmp/modera-moderate --policy rules-review
```

These give `publish`, `reject` and `review`. The policies match an artificial phrase to
demonstrate the integration, and say nothing about accuracy.

## Photos

```sh
/tmp/modera-moderate --input image < photo.png
printf '%s' 'https://your-allowed-media-host.example/photo.png' | /tmp/modera-moderate --input image-url
```

The rules-only server has no photo backend, so a valid photo comes back `pending` with
a typed backend error. For real results, run the
[reference deployment](../../../../deploy/reference/README.md) with the same key and its
`default` policy. URL input also needs the host on the server's allowlist; the command
sends the URL to Modera and never fetches it. Replace the placeholder host with yours.

Text is read as is, up to 4,096 characters. Photos are read up to 8 MiB and URLs up to
2,048 bytes. Input must finish before the network deadline starts, and Ctrl+C interrupts
a blocked read or cancels a request in flight. The default deadline is 10 seconds across
at most three attempts; change it with `--timeout 5s --attempts 1`. HTTPS is required
outside loopback.

## Using the outcome

| Exit | Disposition | What an application would do |
| --- | --- | --- |
| `0` | `publish` | The configured checks allowed it, so it may be published. |
| `2` | `review` | Hold it and open a review case. |
| `3` | `reject` | Keep it unpublished and run the rejection flow. |
| `4` | `pending` | There is no trustworthy decision. Hold it and retry or surface the failure. |
| `1` | none | Bad flags, input or configuration, or the output could not be written. |

`--help` exits `0` and prints to stderr. Run the built binary when you check exit codes,
because `go run` does not pass through the program's status.

In this example a fallback `allow` becomes `review` and a fallback `block` stays
`reject`. The original action, fallback flag, coverage, request ID and policy and model
provenance remain visible under `result`.

An error produces `pending`, an `error` object and no `result`. For API errors it
includes the code, status, request ID, attempts and whether the failure is retryable.
Authentication and permanent backend errors need an operator, so `pending` does not
mean retry forever. Retries are new evaluations. The command never prints submitted
content, source URLs, credentials or server error text, but results contain scores and
provenance, so apply your own logging policy to them.

CI covers this command with the race detector and static checks, and
`uv run --locked python clients/go/integration.py` runs the built binary against the real
API to check all four dispositions and the upload and URL error paths.
