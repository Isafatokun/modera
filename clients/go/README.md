# Modera Go client

A synchronous, standard-library-only Go client for text, image URLs and image
uploads. It preserves decisions, findings, policy/model versions, request IDs,
coverage, rules provenance and fallback status. It creates no stored jobs or
moderation actions. Go 1.23+ language compatibility; CI uses Go 1.27.3.

The module lives at `github.com/Isafatokun/modera/clients/go`. It is not yet tagged.
To consume an untagged revision from an application, use `go get` with a reviewed
repository commit. This installation was checked in a fresh external module:

```sh
go get github.com/Isafatokun/modera/clients/go@2a813900bf28e56efa9d04bbf5e5aec2a354dcd9
```

Go generates the pseudo-version; do not construct it manually. CI also installs
each PR's exact head into a fresh consumer with no local replacement and compiles
all three SDK methods. From the repository root, reproduce it with:

```sh
uv run --locked python clients/go/consumer_check.py --revision FULL_COMMIT_SHA
```

The declared minimum Go 1.23 language compatibility is checked separately from the
current Go 1.27.3 race/static checks. Use a maintained Go release for deployment;
the older toolchain job checks compatibility, not ongoing security support.

A [runnable application example](cmd/moderate/README.md) handles text, photo stdin
and image URLs with JSON dispositions and documented exit codes. Inside this checkout:

```sh
cd clients/go
go test -race ./...
go vet ./...
go run honnef.co/go/tools/cmd/staticcheck@v0.8.2 ./...
```

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
    // Keep content pending or follow your explicit application failure policy.
    // An error does not contain an allow decision.
    return err
}
// result.Fallback and result.Policy identify any operator-configured reduced policy.
switch result.Decision.Action {
case "allow":
    // Apply the application's allowed-content behavior.
case "review":
    // Create a review case in the application, if that is its chosen workflow.
case "block":
    // Apply the application's blocked-content behavior.
}
```

Import the package as `modera "github.com/Isafatokun/modera/clients/go"`, together
with `context`, `os` and `time`. The service must have a matching configured policy
and backend; the client does not choose or download classifiers.

`ModerateImageURL(ctx, url, policy)` sends the URL to Modera, without fetching it
locally. Server host/DNS restrictions still apply. `ModerateImage(ctx, imageBytes,
policy)` snapshots an image of 1 byte–8 MiB into a replayable multipart body.
Do not mutate the source byte slice during the initial copy. Filenames/MIME types
do not determine whether an image is valid; the server decodes the content.
Empty policy selects `default`. Text is preserved and never token-truncated.

## Bounds and retries

- Default: at most three logical attempts, a 30-second overall network/retry
  context, base backoff 200 ms and maximum backoff two seconds, with full jitter.
  Options permit one to five attempts. The caller's earlier deadline always wins.
- `AttemptTimeout` defaults to zero: an attempt uses the remaining overall budget.
  Set it explicitly if attempts must finish sooner. Cancellation applies to active
  network I/O and retry waits using request contexts and the standard HTTP transport.
  Supplied transports must honor request contexts. Bounded local input preparation
  precedes the network/retry context.
- Retries cover transport timeouts, interrupted connections (EOF/reset/broken
  pipe), refused connections and valid API errors marked retryable
  on 408, 429 or 503. Ordinary validation/authentication errors, malformed responses,
  invalid certificates, unsupported backends and redirects are not retried.
- Integer and HTTP-date `Retry-After` values are respected as minimum delays; they
  are not shortened to the backoff cap. If the server's delay cannot fit in the
  remaining budget, the client immediately returns the last typed `APIError`.
- Upload bytes and multipart boundaries are identical across retries. Each attempt
  is a new server evaluation with a new request ID, **not** a deduplicated result
  replay. Image URL contents and server model/policy versions may change between
  attempts. The application owns business-action idempotency.

Reuse a client across goroutines. A custom `HTTPClient` is copied rather than
modified; redirects are always disabled, including when one is supplied.
HTTPS is required outside loopback by default. For a trusted private service
network using plaintext HTTP, opt in explicitly with `Options{AllowHTTP: true}`,
for example when addressing `http://modera:8000` inside a Compose network.
Credentials remain in the Authorization header and are omitted from client/error
formatting. URL credentials, queries and fragments are rejected.

## Error handling

Use `errors.As(err, &apiError)` with `var apiError *modera.APIError` to inspect
`Status`, `Code`, `RequestID`, effective `Retryable`, `RetryAfter`, and `Attempts`.
Use `errors.Is` for `context.Canceled` and `context.DeadlineExceeded`, including
an exhausted per-attempt context deadline. A custom HTTP client or transport can
report its own timeout independently of these contexts.
`ProtocolError` means the client received no trustworthy API decision.

Responses are bounded to 1 MiB and checked for action/coverage/finding/provenance
consistency, required scores, expected modality and request-ID agreement. A changed
policy ID is accepted only with explicit fallback provenance. Scores remain model
outputs, not calibrated safety guarantees. Unknown additional response fields are
ignored for compatibility; known contract fields remain validated. No local rule
fallback is silently applied, and unexpected/unparseable responses never become
allow decisions. Untrusted server error messages/bodies are not included in errors.

## Live contract check

From the repository root, with Python development dependencies and Go on PATH:

```sh
uv run --locked python clients/go/integration.py
```

This Linux helper starts the actual local rules-only API on a reserved loopback
socket with a fresh key, runs `go run ./cmd/check`, then terminates it. It checks
rules block/allow, rules/model provenance, distinct request IDs, uploaded PNG
handling, unconfigured-image errors, disallowed image URLs and authentication.
It does not claim model quality or start a public deployment.
