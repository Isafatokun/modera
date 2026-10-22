# Moderation API

`POST /v1/moderations`

Request validation is available. Valid requests return `501 Not Implemented`:

```json
{
  "error": {
    "code": "moderation_not_implemented",
    "message": "Moderation is not implemented.",
    "param": null,
    "retryable": false,
    "request_id": "req_0123456789abcdef0123456789abcdef"
  }
}
```

No content is classified, no image URL is fetched, and no moderation decision is returned.
Authentication, image decoding, and policy resolution are pending. Upload validation
checks the request structure and byte count, not whether the bytes contain a valid image.

## JSON requests

Content type: `application/json`.

Text:

```json
{
  "input": {
    "type": "text",
    "text": "Hello everyone"
  },
  "policy": "default"
}
```

Image URL:

```json
{
  "input": {
    "type": "image",
    "source": {
      "type": "url",
      "url": "https://media.example.com/photo.jpg"
    }
  },
  "policy": "default"
}
```

Exactly one input is required. The `input.type` field selects its schema. Unknown fields,
conflicting fields, base64 input, audio, video, and input arrays are rejected.
URLs must use HTTPS and cannot contain credentials or fragments. URL syntax validation
does not establish that a destination is safe to fetch.

## File uploads

Content type: `multipart/form-data`, with a boundary supplied by the HTTP client.

| Field | Required | Value |
| --- | --- | --- |
| `file` | Yes | One non-empty image file |
| `policy` | No | Policy identifier; defaults to `default` |

```sh
curl http://127.0.0.1:8000/v1/moderations \
  -F 'file=@photo.jpg' \
  -F 'policy=default'
```

The multipart encoding implies image input. No `type` or nested `input` form field is
needed. Text moderation uses JSON. Unknown and duplicate form fields are rejected.
Uploaded files are closed after request validation; they are not retained as stored assets.

## Limits

| Item | Limit |
| --- | --- |
| Text | 1–4,096 Unicode characters; whitespace-only text rejected |
| Image URL | 2,048 characters |
| Image file | 1 byte–8 MiB |
| Complete JSON body | 32 KiB |
| Complete multipart body | 9 MiB, including boundaries and headers |
| Body read and validation deadline | 15 seconds |
| Policy identifier | 1–64 ASCII letters, digits, underscores, or hyphens |

Body limits are enforced while reading, including requests without `Content-Length`.
Compressed request bodies are not supported. The policy field is currently validated
as an identifier only; named policies are not yet loaded or applied.

## Moderation result (schema only)

`ModerationResult` defines a completed evaluation. It is included in OpenAPI components
but is not currently returned by the endpoint. Valid requests still return `501`.
The following scores and model identifiers are illustrative:

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

| Field | Meaning |
| --- | --- |
| `request_id` | Same request identifier as `X-Request-ID` |
| `input_type` | `text` or `image`, independent of how an image was supplied |
| `policy` | Applied policy identifier and version; the version is a string |
| `decision.action` | `allow`, `review`, or `block` under the applied policy |
| `decision.reasons` | Policy reason codes; empty for allow, required for review/block |
| `findings` | One category score per evaluated check |
| `coverage.evaluated` | Categories successfully evaluated |
| `coverage.unsupported` | Relevant categories the selected processing cannot evaluate |
| `coverage.not_applicable` | Categories that do not apply to this input |
| `models` | Non-empty list of model identifiers and revisions used |
| `preprocessing_version` | Version of the input preparation pipeline |

Scores are finite numbers from 0 to 1. They are model outputs, not guaranteed
probabilities or values that can be compared across models. Findings describe model
outputs; the policy determines the action. Reason codes may identify policy rules
rather than individual categories. Categories and reason codes are extensible strings.

Coverage lists must be disjoint and contain no duplicates. Every evaluated category
must have exactly one finding. Unsupported and not-applicable categories have no scores;
they must not be represented as zero-score findings. At least one check must be evaluated.

These models validate result structure and consistency. The policy evaluator, which
is not implemented yet, must enforce required coverage: an unsupported required check
invalidates the policy/input combination; a failed required check returns an error.
Neither condition permits an allow decision. Optional unsupported checks remain explicit.

`review` recommends human review; it does not create or store a review case. Allow
means the configured checks passed, not that the content is guaranteed safe. Version
metadata records provenance; it does not alone guarantee reproducible model outputs.
Completed allow, review, and block decisions will use HTTP `200` once implemented.

## Current responses

| Status | Meaning |
| --- | --- |
| `400` | Malformed JSON or multipart, invalid fields, missing/empty file, or exceeded field/file count |
| `408` | Request body deadline exceeded |
| `413` | Request body or uploaded file exceeds its byte limit |
| `415` | Unsupported request content type or content encoding |
| `500` | Unexpected server error; no moderation decision |
| `501` | Request is valid; moderation is not implemented |

`422` is reserved for media that cannot be processed, such as corrupt image bytes;
image decoding is not implemented yet.

## Errors and request IDs

All application errors use the `error` envelope shown above. HTTP status identifies
the broad failure; `error.code` identifies the specific condition. Do not parse
`message` for control flow. Error responses include `Cache-Control: no-store`.

| Code | Status | Retryable |
| --- | --- | --- |
| `invalid_request` | `400` | No |
| `invalid_json` | `400` | No |
| `invalid_multipart` | `400` | No |
| `unsupported_input_type` | `400` | No |
| `request_timeout` | `408` | Yes |
| `payload_too_large` | `413` | No |
| `unsupported_media_type` | `415` | No |
| `unsupported_content_encoding` | `415` | No |
| `internal_error` | `500` | No |
| `moderation_not_implemented` | `501` | No |
| `service_not_ready` | `503` on `/health/ready` | Yes |

Validation reports the first error. `param` identifies the affected field, such as
`input.text`, or its nearest known parent; it is `null` when no field applies.
Submitted values and unknown field names are omitted. Unexpected server errors
return a generic message and log only the request ID and exception type.

Every application HTTP response includes a generated `X-Request-ID`. For errors,
the same value appears in `error.request_id`. Incoming request IDs are not reused.
Routing errors such as `404` and `405` use the same envelope with `not_found` and
`method_not_allowed` codes; protocol headers such as `Allow` are preserved.

`retryable: true` means another attempt may succeed, not that work was queued or
that a retry will succeed. Respect `Retry-After` when present and bound retries.
Modera does not retry or resume work automatically. Readiness failures currently
include `Retry-After: 1`.

OpenAPI is available at `/openapi.json` when `MODERA_DOCS_ENABLED=true`.
