# Moderation API

`POST /v1/moderations`

Request validation is available. Valid requests return `501 Not Implemented`:

```json
{"detail": "moderation is not implemented"}
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

## Current responses

| Status | Meaning |
| --- | --- |
| `400` | Malformed JSON or multipart, invalid fields, missing/empty file, or exceeded field/file count |
| `408` | Request body deadline exceeded |
| `413` | Request body or uploaded file exceeds its byte limit |
| `415` | Unsupported request content type or content encoding |
| `501` | Request is valid; moderation is not implemented |

`422` is reserved for media that cannot be processed, such as corrupt image bytes;
image decoding is not implemented yet.

The shared moderation result and error contracts are not finalized. Validation
responses omit submitted field values. OpenAPI is available at `/openapi.json`
when `MODERA_DOCS_ENABLED=true`.
