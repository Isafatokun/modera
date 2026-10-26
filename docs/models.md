# Models and rules

Modera separates adapters (how a model runs), label mappings (what its outputs mean),
and policies (which scores trigger decisions). Text/photo classifiers are supported;
generative guards and object detectors require different adapters. No model is
selected automatically. Validate category meanings, language coverage and thresholds
before changing models.

## Classifiers

Install CPU inference dependencies with `uv sync --locked --extra inference`.
Download the selected checkpoint before starting Modera, using the same cache:

```sh
uv run --locked --extra inference hf download \
  Horizon-Labs/multilingual-toxicity-small \
  --revision 01adcc628a33848569816d4851a38544c321b92c \
  --include '*.json' '*.safetensors' '*.txt' '*.model'
```

Set model ID, full revision, and native-label-to-category mapping as shown in
[.env.example](../.env.example), plus `MODERA_POLICY_FILE`. Restart to change models.
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
per modality per process, with no waiting queue. Text and photos can run concurrently,
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
