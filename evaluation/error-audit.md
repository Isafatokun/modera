# Error audit — 29 October 2026

The sampled text errors reproduce directly from the pinned model checkpoints.
**No label-index, sigmoid/softmax or API score discrepancy was found in the
48 replays.** The evidence points to model generalization limits, differing label
semantics and some questionable annotations. This does not prove the entire
pipeline is defect-free or quantify the prevalence of annotation errors.

## What was checked

Select six errors per model/source/error stratum using `audit-v1:` salted case-ID
SHA-256 ordering: two models × two sources × false positives/negatives = 48
model-case observations (some inputs occur for both models). No hand selection by
score or desired conclusion. This is an error-conditioned qualitative sample,
not a random sample of all messages or an independently adjudicated label audit.

For every sampled input, reload the pinned checkpoint through Transformers
independently of Modera's serving adapter, use its native tokenizer without
truncation, locate the toxicity label by the checkpoint's label map, and apply
sigmoid for the multi-label Horizon head or softmax for Gravitee's single-label
head. All 48 scores equal the saved API scores exactly; maximum absolute
difference is **0.0**. All sampled inputs fit the 256-token setting.

Static inspection also confirms `TextInput` validates blank/length constraints
and returns the original string; the text request path forwards that string to
the classifier. It does not lowercase, strip accents, truncate or translate it.
Horizon maps label 0 (`toxicity`) under `multi_label_classification`; Gravitee maps
label 1 (`toxic`) under `single_label_classification`. The other Horizon labels
are deliberately omitted by the current candidate mapping, not mistaken for
its toxicity output.

## Findings from reading the sampled text

| Finding | Observed examples | Implication |
| --- | --- | --- |
| Implicit abuse is missed | Discriminatory stereotypes, denial of autonomy, veiled humiliation and sarcastic hostility receive low scores | Test contextual understanding, rather than only profanity recall |
| Benign discussion is flagged | Counterspeech, reporting persecution, a disability-support statement and a courteous French goodbye | Measure false flags on identity mentions, negation, reporting and ordinary conversation |
| Some French labels need adjudication | Case `toxifrench-7463af214f7956859cd3d1d376109db932c41a34148a6cbed88edb4f3509b2ff` is labelled non-toxic despite a direct insult and an accompanying generated explanation describing strong hostility | A dataset-negative model escalation is not automatically a genuine moderation mistake |
| Scope differs across annotations | Familiar vulgar banter is sometimes negative; subtle mockery or stereotypes sometimes positive | Define application categories before selecting thresholds |
| Metadata can mislead demographic slices | One sampled ToxiGen record's target-group metadata does not match the group explicitly mentioned in the text | Do not publish demographic disparity rates from unverified group metadata |

The qualitative judgments above are assistant review flags, not new ground truth.
French auxiliary explanations are generated annotation artifacts and are not
independent human evidence. They may contradict the binary label, the text or
their own category codes. Do not replace binary labels using their scores or
explanations. Source labels and all previous benchmark counts remain unchanged.

## Label sensitivity does not explain away the English misses

The original mapping was mean human toxicity rating >=3 on a 1–5 scale. As a
post-hoc diagnostic, changing the boundary to >2.5 changes 29 of the 466 test
labels. It does not resolve low recall. Also inspecting only extreme mean ratings
(<=2 or >=4) leaves 372 cases; these extreme means do **not** prove individual
annotators agreed.

| Model | Original English recall | At >2.5 labels | On extreme mean ratings | False positives on extreme means |
| --- | ---: | ---: | ---: | ---: |
| Horizon Small | 24.4% | 23.4% | 27.4% | 8.0% |
| Gravitee DistilBERT | 33.3% | 32.1% | 33.3% | 10.5% |

No new cutoff was fitted or acceptance gate rerun on these post-hoc subsets.
They are diagnostic observations, not new independent tests.

Ranking also matters: French ROC AUC is 0.841 for Horizon and 0.764 for Gravitee;
English is 0.728 and 0.698 respectively on the existing held-out cases. Gravitee's
higher recall at 0.5 does not establish better ranking or make it the preferred
model under a tight false-positive budget. These are small, reused benchmark
samples, not statistical significance or production-performance claims.

## Image audit

Among the 42 source-labelled unsafe contextual sexual-safety images, the model's
highest-scoring category was `safe` for 32, `sexy` for seven, `porn` for two and
`hentai` for one. These are model predictions, not independent image annotations.
The unchanged policy blocks porn/hentai at 0.95 and ignores `sexy`.

The benchmark's paper studies harm from the combination of image and rendered
text, including cases whose individual modalities can be benign. It therefore
cannot validate the explicit-nudity categories or choose their thresholds.
No new visual ground truth or manual per-image adjudication was created in this
audit. The 1/42 contextual-safety detection result remains a coverage diagnostic.

## Next experiment selected from the findings

1. Establish a small **independently reviewed** English/French corpus with clear
   labels for targeted abuse, discrimination, threats, sexual content, benign
   discussion, quoting/counterspeech and insufficient context. Keep disagreement
   explicit. Reserve complete source/thread/template families for later testing.
2. Use `textdetox/xlmr-large-toxicity-classifier-v2` as the next capacity baseline.
   It has a compatible sequence-classification architecture but shares the
   TextDetox task/domain; its published scores are not independent proof of better
   generalization. Pin its revision before any run. Measure CPU memory and deadlines.
3. Separately explore `Qwen/Qwen3Guard-Gen-0.6B` for contextual moderation. Its
   prompt/response safety taxonomy differs from community-comment toxicity. It
   needs a dedicated generative adapter, bounded output parsing and instruction-
   injection testing; it cannot be dropped into the existing classifier loader or
   have its Safe/Controversial/Unsafe output treated as calibrated probabilities.
4. Obtain photo labels aligned with actual policy categories, including explicit
   nudity, suggestive clothing and benign photos. Evaluate OCR/contextual sexual
   harm separately. The current image challenge cannot supply those labels.

These are research candidates, not recommended replacements. No replacement model
was run or downloaded. Production thresholds, model configuration and runtime
behavior remain unchanged. The present task was to diagnose mistakes
before choosing another experiment.

## Reproduce

From the external evaluation checkout, with its prepared model/data cache and
original result directories:

```sh
uv run --locked --extra inference python -m evaluation.error_audit \
  --directory evaluation/data/external-v1 \
  --runs evaluation/results/external-v1 --cache model-cache \
  --output evaluation/results/error-audit-v1
```

Existing output directories are rejected. Source fingerprints, result IDs and
recorded model settings are checked before replay. The report preserves sampled
case IDs, input hashes, token counts, paired scores, annotation-boundary sensitivity
and per-source ranking metrics without redistributing raw text.

[Machine-readable evidence](error-audit-results.json).

Sources:

- Existing pinned ToxiFrench/ToxiGen sources: [evaluation protocol](external.md).
- Image benchmark: https://aclanthology.org/2025.acl-long.234/
- Capacity baseline: https://huggingface.co/textdetox/xlmr-large-toxicity-classifier-v2
- Contextual guard: https://huggingface.co/Qwen/Qwen3Guard-Gen-0.6B
- Qwen guard implementation: https://github.com/QwenLM/Qwen3Guard
