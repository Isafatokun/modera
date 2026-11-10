# Moderation API

`POST /v1/moderations` — `Authorization: Bearer <api-key>` required.

See [how it works](how-it-works.md) for the flow and your responsibilities as the
caller. An input type with no model or rules backend returns
`501 moderation_not_implemented`; see [models](models.md).

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

One input per request, and `policy` defaults to `default`. Unknown or conflicting
fields, duplicate form fields, base64, audio, video and batches are rejected.

Image URLs must be HTTPS on port 443 with no credentials or fragments, and the host
must be allowlisted, for example `MODERA_IMAGE_ALLOWED_HOSTS='["media.example.com"]'`.
Downloads are off by default. Modera rejects private or internal DNS answers,
redirects, environment proxies and compressed responses, and verifies TLS.
Uploaded and downloaded images must decode as a still JPEG, PNG or WebP; the filename
and MIME type are not trusted. Images are not stored.

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

Photo preparation and inference run off the event loop, with bounded queues; see
[operations](operations.md#overload).

An interrupted or stalled download restarts once from byte zero with jitter, and each
attempt resolves and validates the destination again. Permanent failures are not
retried, and a source `Retry-After` longer than one second is returned to the caller
instead of being waited out.

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
| `500` | `internal_error`, `model_output_invalid`, `model_evaluation_failed`; non-retryable, no decision |
| `501` | `moderation_not_implemented`; no backend configured, not retryable |
| `503` | `service_not_ready`, `service_busy`, `queue_wait_timeout`, `image_processing_timeout`, `input_fetch_failed`, `check_unavailable`; retryable, `Retry-After: 1` |

Policy [coverage failures](policies.md) return errors rather than an allow decision.

Use `code`, not message text, for control flow. Validation reports the first error;
`param` is its known field/parent or `null`. Submitted values and unknown field names
are omitted. Unexpected errors log only request ID and exception type. Errors include
`Cache-Control: no-store`; their request ID matches the `X-Request-ID` header.

`retryable` means another attempt may succeed. Respect `Retry-After` and keep attempts
bounded, with jitter and an overall deadline. A retry is a new evaluation with a new
request ID, and the URL's bytes or the configuration can change between attempts; see
[how it works](how-it-works.md#failures-and-retries).

OpenAPI is served at `/openapi.json` when documentation is enabled. Health checks and
the documentation pages need no key.

When multiple image models are configured, model findings also contain `model`,
matching an ID in `models`. Triggered composite rules contributing to the selected
action appear in `decision.matched_rules`; reasons remain category names. These
fields are omitted when unused. See [combining models](combining-models.md).
