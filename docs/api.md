# Moderation API

`POST /v1/moderations` — `Authorization: Bearer <api-key>` required.

Configured backends return a policy decision. A modality with no model or rules
backend returns `501 moderation_not_implemented`. See [model setup](models.md).

## Requests

JSON text (`Content-Type: application/json`):

```json
{"input":{"type":"text","text":"Hello everyone"},"policy":"default"}
```

JSON image URL:

```json
{"input":{"type":"image","source":{"type":"url","url":"https://media.example.com/photo.jpg"}},"policy":"default"}
```

Image file (`multipart/form-data`; let the client set the boundary):

```sh
curl http://127.0.0.1:8000/v1/moderations \
  -H "Authorization: Bearer $MODERA_API_KEY" \
  -F 'file=@photo.jpg' -F 'policy=default'
```

One input per request. `policy` defaults to `default`. Unknown/conflicting fields,
duplicate form fields, base64, audio, video, and batches are rejected. Image URLs
require HTTPS on port 443 and cannot contain credentials or fragments. Configure exact
hosts, for example `MODERA_IMAGE_ALLOWED_HOSTS='["media.example.com"]'`; downloads
are disabled by default. Private/internal DNS answers, redirects, environment proxies,
and response compression are rejected. TLS certificates and hostnames are verified.
Uploaded and downloaded images must decode as still JPEG, PNG, or WebP. Filename and MIME type are not trusted.
Files are closed after reading; image data is not retained as a stored asset.

| Limit | Value |
| --- | --- |
| Text | 1–4,096 Unicode characters; not whitespace-only |
| Image URL | 2,048 characters |
| Image file | 1 byte–8 MiB |
| Decoded image | 16 million pixels |
| Active moderation requests | 8/process by default; excess requests get `503` before body parsing |
| Request body | 32 KiB JSON; 9 MiB multipart including headers/boundaries |
| Deadlines | 15 seconds for the body; 5 seconds for image validation |
| Downloads | 4 per instance, no waiting queue; 20-second total deadline |
| Download recovery | At most 2 attempts; 8 seconds each, 3-second inactivity timeout |
| Inference | One slot/modality/process; default 5-second deadline; text model defaults to 256 tokens |
| Policy name | 1–64 ASCII letters, digits, underscores, or hyphens |

Body limits apply without `Content-Length`. Compressed bodies are unsupported.
With a policy file, names and input types are checked; without one, only identifier
syntax is validated. See [policy configuration](policies.md).

Photo validation/preparation and inference run off the event loop. Each stage
permits five waiting jobs by default, with up to two seconds of queue waiting
within its deadline. Full queues return `503 service_busy`; expired waits return
`503 queue_wait_timeout`. Timed-out native work keeps its slot until completion.

Interrupted or stalled downloads restart once from byte zero with jitter; partial bytes
are discarded. Each attempt resolves and validates the destination again. Permanent
failures are not retried. Longer source `Retry-After` delays are returned to the caller
instead of waiting inside the request. This is request-scoped recovery; progress does not survive
a service restart.

## Moderation result

Example scores and model IDs:

```json
{
  "request_id": "req_0123456789abcdef0123456789abcdef",
  "input_type": "image",
  "policy": {"id": "default", "version": "1"},
  "decision": {"action": "review", "reasons": ["violence"]},
  "findings": [
    {"category": "violence", "score": 0.62},
    {"category": "sexual", "score": 0.01}
  ],
  "coverage": {
    "evaluated": ["violence", "sexual"],
    "unsupported": ["hate"],
    "not_applicable": []
  },
  "models": [{"id": "image-classifier", "revision": "example-1"}],
  "preprocessing_version": "image-v1"
}
```

Completed evaluations return `200` for `allow`, `review`, or `block`. Review
recommends human review; it does not create a case. Allow means the policy passed,
not guaranteed safety. Reasons are empty for allow and required otherwise.

Every evaluated category needs one finite score from 0 to 1. Unsupported and
not-applicable categories have no scores. Coverage lists are disjoint; at least one
check must be evaluated. Scores are model outputs, not guaranteed probabilities or
comparable across models. Category/reason codes are extensible strings. Version
metadata records provenance, not guaranteed reproducibility.

Each finding includes `source: "model"` or `"rules"`. Rule results include
`rules_version`; `models` lists only models that completed evaluation and can be empty
for rules-only results. `fallback: true` identifies degraded evaluation; `policy`
records the policy actually used. The preprocessing version fingerprints model
settings, label mappings, and rule configuration. See [fallback semantics](models.md).

## Errors

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Text must not be blank.",
    "param": "input.text",
    "retryable": false,
    "request_id": "req_0123456789abcdef0123456789abcdef"
  }
}
```

| Status | Codes / meaning |
| --- | --- |
| `400` | `invalid_request`, `invalid_json`, `invalid_multipart`, `unsupported_input_type`, `unknown_policy`, `policy_not_applicable`, `policy_unsupported`, `image_url_not_allowed` |
| `401` | `authentication_required`; includes `WWW-Authenticate: Bearer` |
| `404` / `405` | `not_found` / `method_not_allowed` |
| `408` | `request_timeout`; retryable |
| `413` | `payload_too_large`, `image_too_large` |
| `415` | `unsupported_media_type`, `unsupported_content_encoding`, `unsupported_image_format`, `animated_image_not_supported` |
| `422` | `input_too_long` (model token limit), `invalid_image` (undecodable data), `input_fetch_rejected` (source rejection, redirect, invalid response, or TLS failure) |
| `500` | `internal_error`; no decision |
| `501` | `moderation_not_implemented`; no backend configured, not retryable |
| `503` | `service_not_ready`, `service_busy`, `queue_wait_timeout`, `image_processing_timeout`, `input_fetch_failed`, `check_unavailable`; retryable, `Retry-After: 1` |

Policy [coverage failures](policies.md) return errors rather than an allow decision.

Use `code`, not message text, for control flow. Validation reports the first error;
`param` is its known field/parent or `null`. Submitted values and unknown field names
are omitted. Unexpected errors log only request ID and exception type. Errors include
`Cache-Control: no-store`; their request ID matches the `X-Request-ID` header.

Retryable means another attempt may succeed. Respect `Retry-After`, use bounded
attempts with jitter and an overall deadline. A retry runs a new evaluation with
a new request ID; requests are not deduplicated or replayed. Modera stores no
result or business action, so retrying creates no duplicate moderation side effect.
URL bytes and model/policy configuration can change between attempts. Pending
work does not survive restart. OpenAPI: `/openapi.json` when enabled, with bearer
authentication documented. Health checks and enabled documentation are unauthenticated.
