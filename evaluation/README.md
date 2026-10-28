# Text quality evaluation

Compare pinned classifiers using the real CPU FP32 adapter and unchanged 256-token
limit. Serving defaults are not changed. This is model/policy evaluation, not HTTP
capacity or latency measurement.

```sh
uv sync --locked --extra inference --group evaluation
uv run --locked --extra inference --group evaluation python -m evaluation.compare_text prepare \
  --directory evaluation/data/publisher-test --cache model-cache
uv run --locked --extra inference --group evaluation python -m evaluation.compare_text models \
  --cache model-cache
uv run --locked --extra inference --group evaluation python -m evaluation.compare_text run \
  --directory evaluation/data/publisher-test --candidate evaluation/candidates/horizon-small.json \
  --cache model-cache --output evaluation/results/horizon-small
uv run --locked --extra inference --group evaluation python -m evaluation.compare_text run \
  --directory evaluation/data/publisher-test --candidate evaluation/candidates/gravitee-distilbert.json \
  --cache model-cache --output evaluation/results/gravitee-distilbert
```

Preparation removes normalized exact matches with the publisher's training data,
conflicting-label groups and duplicate test texts, then selects 50 examples per
language/label by normalized-text SHA-256. It freezes the sample hash before inference.
Existing directories are rejected; interrupted preparation can use a new directory
with the same download cache. No threshold tuning, retries or silent truncation.

The threshold is fixed at 0.5, representing review **or** block under the reference
policy, not automatic blocking. Raw scores/errors are flushed after every case;
only a complete run writes `report.json`. Reports retain failures, per-language
confusion matrices, sample/model fingerprints and library versions. End-to-end
recall includes failed cases; undefined ratios are `null`. Scores are not calibrated
or interchangeable across models.

The **Text quality comparison** workflow repeats this on a fresh runner and uploads
reports and predictions for 30 days, including partial predictions on failure.
It uploads no raw texts, credentials or model weights. Local `data/` and `results/`
are ignored by Git. See [results and limitations](results.md).

Source: [publisher dataset](https://huggingface.co/datasets/gravitee-io/textdetox-multilingual-toxicity-dataset),
revision `bc570f7558ab8cbd8d0d9a550c7406e8343fad90`, derived from TextDetox.
The dataset and Gravitee classifier declare OpenRAIL++; Horizon declares Apache-2.0.
Model terms are separate from Modera's code licence. This is a publisher-test
comparison, not a new independent benchmark. Exact-match filtering cannot audit
paraphrases, related sources or pretraining. Balanced samples do not represent
production prevalence, and 50 examples per class/language give limited precision.
Do not tune thresholds on this test and reuse it as independent validation.
