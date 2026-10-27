# Models and rules

Modera separates adapters (how a model runs), label mappings (what its outputs mean),
and policies (which scores trigger decisions). Text/photo classifiers are supported;
generative guards and object detectors require different adapters. No model is
selected automatically. Validate category meanings, language coverage and thresholds
before changing models.

## Classifiers

Install CPU inference dependencies with `uv sync --locked --extra inference`.
Copy [examples/models.json](../examples/models.json) and set the model IDs, pinned
revisions and native label mappings you want to use. Set a modality to `null` to
disable it; at least one model is required. Omit the model file for rules-only
deployments. These are configuration examples, not recommended models or safety
thresholds. Prepare the configured checkpoints before starting Modera:

```sh
uv run --locked --extra inference python -m modera.prepare_models --config examples/models.json
export MODERA_MODEL_FILE=examples/models.json
export MODERA_POLICY_FILE=examples/policies.json
uv run --locked --extra inference uvicorn modera.main:create_app --factory
```

Preparation needs no API key. It downloads only configuration/tokenizer files and
safetensors, then loads and warms each classifier using the serving adapter.
Success prints one JSON record per model; a failure exits nonzero. Re-running uses
the Hugging Face cache. Add `--offline` to verify cached files without network access.
This checks architecture and labels, not accuracy, policy thresholds or licences.

`MODERA_API_KEY` is required for serving as usual. Relative `cache_dir` paths in a
model file resolve against that file's directory; omitted paths use the standard
Hugging Face cache. The file is limited to 1 MiB and read once during startup.
Restart to apply changes. Do not combine it with `MODERA_TEXT_MODEL` or
`MODERA_IMAGE_MODEL` settings. Individual environment selections from
[.env.example](../.env.example) remain supported when no model file is set.
Text defaults to sigmoid and 256 tokens; photos default to softmax. Override
`MODERA_TEXT_MODEL__ACTIVATION`, `__MAX_TOKENS` or
`MODERA_IMAGE_MODEL__ACTIVATION` to match the selected model. Text beyond the token
limit returns `422 input_too_long`; content is never silently truncated.

Only models needed by the selected policy run on a request; rules-only policies
do not depend on model inference. Weights load once at startup, on CPU, from the
local cache only. Loading requires safetensors and disables remote model code.
Models load and warm on their serving workers before readiness; missing
weights, incompatible architecture and invalid label mappings normally fail startup.
Telemetry is disabled before importing inference libraries. Inference uses one slot
per modality per process; photos have [bounded waiting](operations.md), while text
rejects immediately when busy. Text and photos can run concurrently,
using their existing model instances. Threads default to 2 per model operation;
the deadline defaults to 5 seconds
(`MODERA_INFERENCE_THREADS`, `MODERA_INFERENCE_TIMEOUT_SECONDS`). Timed-out native
work retains its modality's slot until completion. Busy/failed inference returns
retryable `503`. Both model operations share the process's CPU and memory; concurrent
inference can use twice the configured intra-operation threads. Set CPU/memory limits
at deployment and measure mixed traffic before increasing threads or replicas.

For containers, build with `--build-arg INFERENCE=true`, mount a prepared Hugging Face
cache read-only, and set each model's `__CACHE_DIR` to the mounted cache root. Policy
and rule files must also be mounted. The default image contains no inference packages
or weights. Each process loads its own model copies; account for startup memory peaks.
The initial adapters use FP32; quantization and ONNX execution are not implemented.
When using a model file, mount it read-only and set its cache paths to the mounted
cache location inside the container.
The container runs as UID/GID `10001:10001`; configuration, weights and parent
directories must be readable/traversable by that identity. Prepare the cache as
that user or grant suitable group access before mounting it read-only.

## Text rules and fallback

Set `MODERA_TEXT_RULES_FILE` to a file such as
[examples/text-rules.json](../examples/text-rules.json). Rules work without inference
dependencies or a text model. Select the example `rules-only` policy in requests.
Matching uses literal substrings after NFKC, case-folding and whitespace normalization.
No regex, word-boundary detection or contextual understanding is implied. Limits:
1 MiB configuration, 100 categories, 1,000 phrases total, 256 characters per phrase.

Rule categories must start with `rules.` and cannot stand in for model categories.
Scores are binary: 1 for a match, 0 otherwise, not probabilities. Results identify
`source: "rules"`, `rules_version`, and only the coverage actually evaluated.

Fallback defaults to errors. To explicitly permit a reduced rules-only policy when
text inference fails, is busy, times out, or cannot load:

```sh
export MODERA_TEXT_RULES_FILE=examples/text-rules.json
export MODERA_TEXT_FALLBACK=rules
export MODERA_TEXT_FALLBACK_POLICY=rules-only
```

The fallback policy must contain only configured rule categories. Its actual ID and
version appear in `policy`, alongside `fallback: true` and `models: []`. This setting
applies to all text requests when inference is unavailable. Without an explicit
fallback policy, the requested policy still applies: missing model checks return
`503`, even if no rule matched. Invalid input never triggers model fallback.
Readiness permits unavailable cached text weights or missing inference dependencies
only when rules fallback is explicitly enabled. Invalid classifier configuration
(including label mappings) and photo-model startup failures remain fatal. Restart to
retry loading an unavailable model.

If Modera itself is unreachable, it cannot run fallback. The caller must retry, keep
content pending, or explicitly run its own reduced rules policy. A local rules result
must not be recorded as a completed Modera/model evaluation. A no-match rules result
only means the configured literal phrases were absent; it does not establish safety.
