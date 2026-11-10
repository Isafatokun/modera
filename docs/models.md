# Models and rules

Modera keeps three things apart: the adapter (how a model runs), the label mapping
(what its outputs mean) and the policy (which scores trigger a decision). It supports
classifiers and compatible photo detectors; generative guards are not supported. No
model is selected for you, so validate category meanings, language coverage and
thresholds before you change models.

Related: [ONNX detectors](onnx.md), [object detectors](detectors.md) and
[combining models](combining-models.md).

## Set up classifiers

Install the CPU dependencies with `uv sync --locked --extra inference`. Copy
[examples/models.json](../examples/models.json) and set the model IDs, pinned revisions
and native label mappings. Set a modality to `null` to disable it; at least one model
is required, and rules-only deployments need no model file. The examples are not
recommended models or thresholds.

Prepare the weights first, then start the service:

```sh
uv run --locked --extra inference python -m modera.prepare_models --config examples/models.json
export MODERA_MODEL_FILE=examples/models.json
export MODERA_POLICY_FILE=examples/policies.json
uv run --locked --extra inference uvicorn modera.main:create_app --factory
```

Preparation needs no API key. It downloads only configuration, tokenizer and
safetensors files, then loads and warms each model with the serving adapter. It prints
one JSON record per model and exits nonzero on failure. Re-running reuses the Hugging
Face cache, and `--offline` verifies the cache without the network. It checks
architecture and labels, not accuracy, thresholds or licences.

### Model file

- Relative `cache_dir` paths resolve against the file's directory. Omitted paths use
  the standard Hugging Face cache.
- The file is read once at startup, is limited to 1 MiB, and cannot be combined with
  `MODERA_TEXT_MODEL` or `MODERA_IMAGE_MODEL`. Individual settings from
  [.env.example](../.env.example) still work when there is no model file.
- Text defaults to sigmoid activation and 256 tokens, and photos to softmax. Override
  with `MODERA_TEXT_MODEL__ACTIVATION`, `__MAX_TOKENS` or
  `MODERA_IMAGE_MODEL__ACTIVATION`. Text over the token limit returns
  `422 input_too_long` and is never truncated.

## How models run

- Only the models the selected policy needs run on a request.
- Weights load once at startup, on CPU, from the local cache only, as safetensors with
  remote model code disabled. Telemetry is off.
- Models load and warm up before the service is ready. Missing weights, an
  incompatible architecture or bad label mappings fail startup.
- Each model has one inference slot per process. Photos queue briefly (see
  [operations](operations.md#overload)); text rejects immediately when busy.
- Threads default to 2 per model and the deadline to 5 seconds, set by
  `MODERA_INFERENCE_THREADS` and `MODERA_INFERENCE_TIMEOUT_SECONDS`. A timed-out call
  keeps its slot until it finishes.
- A busy, timed-out or unavailable model returns a retryable `503`. Invalid model
  output or an unexpected internal failure returns a non-retryable `500` with no
  decision.

Models share the process's CPU and memory, so budget threads and memory for all of
them, set limits where you deploy, and measure mixed traffic before raising threads or
replicas. The Transformers adapters use FP32. ONNX is a separate install and does not
quantize models.

### Containers

Build with `--build-arg INFERENCE=true`, mount a prepared Hugging Face cache read-only,
and point each model's `__CACHE_DIR` at the mounted cache root. Mount policy, rule
and model files too. The default image has no inference packages or weights. The
container runs as UID/GID `10001:10001`, so that user must be able to read the
configuration and cache and traverse their parent directories. Each process loads its
own copy of every model, so account for the startup memory peak.

## Text rules and fallback

Set `MODERA_TEXT_RULES_FILE` to a file like
[examples/text-rules.json](../examples/text-rules.json). Rules need no inference
dependencies or text model; select the example `rules-only` policy in requests.

- Matching is literal substring matching after NFKC, case-folding and whitespace
  normalisation. There is no regex, word-boundary or contextual matching.
- Limits: 1 MiB file, 100 categories, 1,000 phrases, 256 characters per phrase.
- Categories start with `rules.` and cannot stand in for model categories.
- Scores are binary (1 for a match, 0 otherwise). Results carry `source: "rules"` and a
  `rules_version`, and list only the coverage actually evaluated.

A rules result with no match means only that the configured phrases were absent. It
does not show the text is safe.

### Fallback

By default a text-model failure is an error. To allow a reduced rules-only evaluation
when text inference is unavailable, times out or cannot load:

```sh
export MODERA_TEXT_RULES_FILE=examples/text-rules.json
export MODERA_TEXT_FALLBACK=rules
export MODERA_TEXT_FALLBACK_POLICY=rules-only
```

- The fallback policy may contain only configured rule categories. The result shows the
  policy actually used, `fallback: true` and `models: []`.
- Without a fallback policy, the requested policy still applies, so missing model
  checks return `503` even if no rule matched.
- A busy text worker is load, not an outage. It returns `503 service_busy` with
  `Retry-After` and never falls back. Invalid input, invalid model output and internal
  inference errors never fall back either.
- Startup tolerates missing text weights or inference dependencies only when fallback
  is enabled. A bad classifier configuration or a photo-model failure stays fatal.
  Restart to retry loading an unavailable model.

If Modera itself is unreachable it cannot fall back. Retry, keep the content pending,
or run your own rules, and do not record a local rules result as a completed Modera
evaluation.
