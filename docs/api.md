# Moderation API

`POST /v1/moderations` — `Authorization: Bearer <api-key>` required.

Modera replies within the same request. It does not publish content, open review cases
or store anything, so what happens next is your application's job; see
[using the result](#using-the-result). An input type with no model or rules backend
returns `501 moderation_not_implemented`; see [models](models.md).

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

## Using the result

A `200` means the evaluation completed, not that the content was allowed.

| Response | What your application does |
| --- | --- |
| `200` `allow` | No configured check reached a threshold. Continue your normal flow if your own rules agree. |
| `200` `review` | Hold the content and open a review case. Modera does not create one. |
| `200` `block` | Reject the content. |
| error | There is no decision. Keep the content pending, fix the request, or retry. Never treat an error or a timeout as an `allow`. |

An `allow` covers only the checks the policy configured, and `coverage` lists exactly what
ran. Only the models the policy needs run. A required check that is unsupported, or has no
result, is an error and never an allow, and a block threshold wins over a review one.

Text fallback is optional. When it applies, the result has `fallback: true`, and the
policy and coverage are those of the reduced rules-only evaluation. Decide how your
application treats reduced coverage; the [reference upload gate](../examples/upload_gate.py)
sends a fallback `allow` to review and keeps a fallback `block` rejected.

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
| `500` | `internal_error`, `model_output_invalid`, `model_evaluation_failed`, `model_not_loaded`; non-retryable, no decision |
| `501` | `moderation_not_implemented`; no backend configured, not retryable |
| `503` | `service_not_ready`, `service_busy`, `queue_wait_timeout`, `image_processing_timeout`, `input_fetch_failed`, `check_unavailable`; retryable, `Retry-After: 1` |

Policy [coverage failures](policies.md) return errors rather than an allow decision.

Use `code`, not message text, for control flow. Validation reports the first error;
`param` is its known field/parent or `null`. Submitted values and unknown field names
are omitted. Unexpected errors log only request ID and exception type. Errors include
`Cache-Control: no-store`; their request ID matches the `X-Request-ID` header.

`retryable` means another attempt may succeed. `Retry-After` is a minimum wait, not a
schedule. The server sends 1 second for busy, not-ready and timeout errors, 2 seconds when
a policy check did not complete, and passes on a longer delay from a photo source. Every
caller rejected at the same moment gets the same value, so clients that sleep exactly that
long come back together and overload the service again. Wait at least `Retry-After`, add
jitter on top, back off on repeated failures, bound the attempts and keep inside an
overall deadline. The Go client does this.

If the client times out or gets no valid response, treat the content as pending. A retry
is a new evaluation with a new request ID, and the URL's bytes or the configuration can
change between attempts. Modera does not deduplicate requests or replay results, so make
your own follow-up actions (publishing, opening a review case) idempotent, keyed on your
content ID and not on Modera's request ID. In-flight work lives in the process and is lost
on restart; see [operations](operations.md).

OpenAPI is served at `/openapi.json` when documentation is enabled. Health checks and
the documentation pages need no key.

When multiple image models are configured, model findings also contain `model`,
matching an ID in `models`. Triggered composite rules contributing to the selected
action appear in `decision.matched_rules`; reasons remain category names. These
fields are omitted when unused. See [combining models](image-models.md#combine-image-models).
