# Next model experiments

These are CPU FP32 research runs. They do not install new serving defaults,
calibrate thresholds, or establish independent production quality. The original
French/English source files reproduce the previously frozen cohort hashes exactly.
The already-inspected test cases are reused only as diagnostics.

## Frozen design

- XLM-R Large v2: `textdetox/xlmr-large-toxicity-classifier-v2`, revision
  `38ab1b77bd3ff7779777cfa5cda203d9912b14b9`. Uses the existing Modera classifier,
  256-token limit, two CPU threads, five-second serving deadline, and fixed
  0.5 review / 0.85 block policy. Every one of the 1,220 test cases is attempted.
- Qwen3Guard: `Qwen/Qwen3Guard-Gen-0.6B`, revision
  `fada3b2f655b89601929198343c94cd2f64d93cc`. Uses a separate research path with the
  official single user-prompt chat template, greedy generation, at most 128 new
  tokens, at most 2,048 input tokens, and two CPU threads. It does not run through
  Modera's HTTP API and has no production adapter.
- Matched comparison: 50 examples per source/class (200 total), selected by the
  `next-models-v1:` salted ID hash before scoring and without using prior model
  errors. XLM-R reports both its full sample and this matched subset. Qwen runs
  the matched subset only. Balanced rates do not represent traffic prevalence.
- Both Qwen interpretations are declared before inference: strict escalation for
  `Unsafe`, broader escalation for `Unsafe` or `Controversial`. These native
  verdicts are not calibrated toxicity probabilities and the model's prompt-safety
  taxonomy differs from community-comment labels. Neither interpretation is
  selected as a winner after looking at test results.

The Qwen parser accepts exactly the documented two-line severity/category format.
Missing, duplicated, contradictory, unknown or extra-text outputs are failed
checks. A parse failure cannot become an allow decision.

Qwen's `generate(max_time=5)` is a token-boundary generation budget, **not a hard
interrupt**. Tokenization, prefill and decode can overrun it. Total calls over five
seconds are explicitly marked failed and yield no escalation decision, even if
some generated text looks valid. The runner does not claim production deadline
cancellation or measure concurrent capacity. Unbounded native-call recovery and
model isolation would require a separately designed serving adapter.

## Reproduce on a Linux host

The collectors record Linux process peak RSS, CPU quota and platform. Peak RSS
is for each complete research process; it is not minimum deployment memory, a
container limit or comparable to the earlier post-check container snapshots.
Run candidates sequentially for the recorded timing methodology. Per-call timings
include the first call, and paths differ (ASGI HTTP versus direct generation).

```sh
uv sync --locked --extra inference --group evaluation --group benchmark
uv run --locked --extra inference python -m evaluation.next_models prepare
```

If the original frozen `evaluation/data/external-v1` directory is unavailable,
recreate it following [external evaluation](external.md). Its preparation needs
only the original pinned tokenizers; using the documented model-preparation command
also downloads the original weights. The experiment does not require rerunning
original model inference.

```sh
uv run --locked --extra inference python -m evaluation.next_models freeze \
  --output evaluation/data/next-models-v1.json
for candidate in xlmr-large-v2 qwen3guard-0.6b; do
  uv run --locked --extra inference python -m evaluation.next_models run \
    --kind "$candidate" --protocol evaluation/data/next-models-v1.json \
    --output "evaluation/results/next-$candidate"
done
uv run --locked --extra inference python -m evaluation.qwen_probes \
  --output evaluation/results/qwen-probes-v1
```

Preparation downloads pinned JSON/tokenizer/safetensors artifacts only; no remote
model code, pickle checkpoints or optimizer files are loaded. Existing result
paths are rejected. Source inputs stay in ignored data directories. Results retain
request/observation IDs, input hashes, full API or native-generation outputs,
errors, timing and model/protocol fingerprints. No raw source corpus is committed.

## Sanity and instruction-injection probes

The additional 14 self-authored probes cover two plain benign messages, four
threat/discrimination messages, and eight verdict/role-injection variants of those
four harmful examples. Their content and expected minimum review are written to
a protocol before the model loads. They use the same template, parsing and timing
budget. They are neither independently labelled nor an exhaustive adversarial
benchmark, and cannot prove injection robustness.

## Sources and model terms

- XLM-R author card: https://huggingface.co/textdetox/xlmr-large-toxicity-classifier-v2
  (declares OpenRAIL++; trained on the TextDetox classification task).
- Qwen author card: https://huggingface.co/Qwen/Qwen3Guard-Gen-0.6B
  (declares Apache-2.0 and prompt/response safety severity/categories).
- Qwen implementation: https://github.com/QwenLM/Qwen3Guard

Model terms differ from Modera's code licence. Neither published in-domain scores
nor performance on this inspected cohort establishes an independently validated
replacement profile. New independent labels and untouched source-group holdouts
remain necessary before adopting any model or cutoff.
