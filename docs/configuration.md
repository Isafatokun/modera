# Configuration

Modera reads settings from environment variables with the `MODERA_` prefix. Nested
settings use a double underscore, for example `MODERA_TEXT_MODEL__ID`. `.env` files
are not loaded automatically; set variables in the shell or your secret manager.
`python -m modera.cli doctor` checks a configuration without loading models.

Invalid values stop the server from starting. An input type with no configured
backend returns `501`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MODERA_API_KEY` | required | Bearer key. 32 to 512 printable ASCII characters, no spaces. |
| `MODERA_PREVIOUS_API_KEY` | unset | Old key still accepted during a [rotation](operations.md#rotate-the-api-key). Must differ from the current key. |
| `MODERA_POLICY_FILE` | unset | Path to the [policy](policies.md) JSON. Required when any model or rule is configured. |
| `MODERA_MODEL_FILE` | unset | Path to a [model](models.md) JSON file. Cannot be combined with the individual model settings below. |
| `MODERA_TEXT_MODEL__*` | unset | One text classifier: `ID`, `REVISION` (full commit SHA), `LABEL_MAP`, `ACTIVATION`, `MAX_TOKENS`, `CACHE_DIR`. |
| `MODERA_IMAGE_MODEL__*` | unset | One photo model, with the same fields. |
| `MODERA_IMAGE_MODELS` | unset | JSON array of up to four photo models; see [combining models](combining-models.md). Not valid together with `MODERA_IMAGE_MODEL`. |
| `MODERA_TEXT_RULES_FILE` | unset | Literal phrase rules, usable without a model. See [rules and fallback](models.md#text-rules-and-fallback). |
| `MODERA_TEXT_FALLBACK` | `error` | `rules` lets text fall back to rules when the text model is unavailable. |
| `MODERA_TEXT_FALLBACK_POLICY` | unset | Rules-only policy applied during fallback. Requires `MODERA_TEXT_FALLBACK=rules`. |
| `MODERA_IMAGE_ALLOWED_HOSTS` | `[]` | JSON array of exact DNS names photo URLs may be fetched from. Empty disables URL input. |
| `MODERA_MAX_IN_FLIGHT_REQUESTS` | `8` | Moderation requests handled at once per process (1 to 256). Extra requests get `503`. |
| `MODERA_INFERENCE_THREADS` | `2` | CPU threads per model (1 to 32). |
| `MODERA_INFERENCE_TIMEOUT_SECONDS` | `5` | Time budget for all photo models on one request (up to 60). |
| `MODERA_IMAGE_QUEUE_SIZE` | `5` | Photo jobs that may wait for a free worker (0 to 32). `0` rejects when busy. |
| `MODERA_IMAGE_QUEUE_WAIT_SECONDS` | `2` | Longest a photo job waits in that queue (up to 10). |
| `MODERA_DOCS_ENABLED` | `false` | Serve `/docs` and `/openapi.json`. |

Model weights are loaded from a local cache and are never downloaded at startup. See
[models](models.md) for how to prepare them.

Generate a fresh key for each deployment, keep it out of source control and out of
browser or mobile code, and terminate TLS in front of the service.
