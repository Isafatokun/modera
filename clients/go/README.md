# Modera Go client

A synchronous Go client for Modera that uses only the standard library. It supports
text, image URLs and image uploads, and keeps the decision, findings, coverage,
policy and model versions, rules provenance, fallback flag and request ID. It needs
Go 1.23 or later and stores nothing.

The module is `github.com/Isafatokun/modera/clients/go` and is not tagged yet. Install
a reviewed commit, and let Go work out the pseudo-version:

```sh
go get github.com/Isafatokun/modera/clients/go@main
```

CI installs every pull request's exact head into a fresh module and compiles all three
methods. Run the same check with
`uv run --locked python clients/go/consumer_check.py --revision FULL_COMMIT_SHA`.

For a complete program that maps results to publish, review, reject and pending, see
the [example application](cmd/moderate/README.md).

## Use

```go
client, err := modera.New(
    "https://moderation.example.com",
    os.Getenv("MODERA_API_KEY"),
    modera.Options{MaxAttempts: 3, TotalTimeout: 30 * time.Second},
)
if err != nil {
    return err
}
ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
defer cancel()
result, err := client.ModerateText(ctx, "Hello everyone", "default")
if err != nil {
    // No decision was made. Keep the content pending; never treat this as an allow.
    return err
}
switch result.Decision.Action {
case "allow":
    // Publish, if your own rules agree.
case "review":
    // Hold the content and open a review case in your application.
case "block":
    // Reject it.
}
```

Import the package as `modera "github.com/Isafatokun/modera/clients/go"`. The service
must already have a matching policy and backend; the client does not choose models.

- `ModerateText(ctx, text, policy)`: text is sent as is and never truncated.
- `ModerateImageURL(ctx, url, policy)`: Modera fetches the URL, not the client, so the
  server's host allowlist applies.
- `ModerateImage(ctx, bytes, policy)`: 1 byte to 8 MiB, copied into a replayable
  multipart body. The server decodes the content, so the filename and MIME type
  do not matter.

An empty policy selects `default`.

## Retries and limits

| Option | Default | Meaning |
| --- | --- | --- |
| `MaxAttempts` | 3 | Attempts including the first, from 1 to 5. |
| `TotalTimeout` | 30s | Bound on all attempts and retry waits. An earlier deadline on your context wins. |
| `AttemptTimeout` | none | Bound on a single attempt. Without it, an attempt may use the whole remaining budget. |
| `BaseDelay`, `MaxDelay` | 200ms, 2s | Exponential backoff with full jitter. |
| `HTTPClient` | standard | Copied, never modified. Redirects are always disabled. |
| `AllowHTTP` | false | Permit plaintext HTTP outside loopback, for a trusted private network. |

The client retries transport timeouts, interrupted or refused connections, and API
errors marked retryable on 408, 429 or 503. It does not retry validation or
authentication errors, malformed responses, bad certificates, an unsupported backend
or redirects. A `Retry-After` value, in seconds or as an HTTP date, is a minimum delay: the client
waits at least that long and adds jitter on top, so callers rejected together do not
return together. If the delay cannot fit in the remaining budget, the client returns
the last `APIError` at once.

Each attempt is a new evaluation with a new request ID, and the bytes behind an image
URL or the server's configuration can change between attempts. Make your own follow-up
actions idempotent.

A client is safe to share across goroutines. HTTPS is required outside loopback. Your
key stays in the `Authorization` header and is left out of formatted output and
errors, and a base URL or image URL containing credentials, a query or a fragment is
rejected.

## Errors and validation

Use `errors.As` with `*modera.APIError` to read `Status`, `Code`, `RequestID`,
`Retryable`, `RetryAfter` and `Attempts`. Use `errors.Is` for `context.Canceled` and
`context.DeadlineExceeded`. A `*modera.ProtocolError` means the client received no
trustworthy decision.

Responses are capped at 1 MiB and checked for consistency: action against reasons,
coverage and findings, required scores, the expected input type, provenance, and a
request ID that matches the header. A different policy ID is accepted only when the
response is marked as fallback. Unknown extra fields are ignored. Error text from the
server is never copied into errors, and a response the client cannot parse is never
turned into an allow.

## Tests

```sh
cd clients/go
go test -race ./...
go vet ./...
go run honnef.co/go/tools/cmd/staticcheck@v0.8.2 ./...
```

`uv run --locked python clients/go/integration.py` from the repository root starts the
real rules-only API on a loopback port with a fresh key and runs the client against it.
It covers block and allow, provenance, distinct request IDs, an uploaded image, an
unconfigured image backend, a disallowed URL and authentication. CI also runs the
module with Go 1.23.12 to check the minimum version.
