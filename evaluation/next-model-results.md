# Next-model results — 29 October 2026

This study reuses the inspected French/English test cohort as a **diagnostic**.
It does not fit thresholds, conduct a fresh acceptance test, or change defaults.
The original source/input hashes reproduced exactly before the experiments.

## Larger classifier: full existing test cohort

At the unchanged 0.5 review cutoff:

| Model | French positives caught / 363 | French dataset-negative flags / 391 | English positives caught / 180 | English dataset-negative flags / 286 |
| --- | ---: | ---: | ---: | ---: |
| Horizon Small (previous run) | 154 (42.4%) | 33 (8.4%) | 44 (24.4%) | 24 (8.4%) |
| Gravitee DistilBERT (previous run) | 246 (67.8%) | 112 (28.6%) | 60 (33.3%) | 37 (12.9%) |
| XLM-R Large v2 | 233 (64.2%) | 72 (18.4%) | 63 (35.0%) | 17 (5.9%) |

All 1,220 XLM-R requests completed without errors through Modera's actual serving
engine via in-process ASGI HTTP. Label mapping is native `toxic` (index 1), with
softmax, FP32 CPU inference, 256-token limit and two threads. The existing
five-second inference deadline was unchanged.

Compared with Gravitee at this cutoff, XLM-R reduces French false flags while
missing slightly more positives; on English it catches three more positives and
flags 20 fewer dataset-negative examples. Compared with Horizon, it catches more
positives in both datasets, with more French false flags. None of these comparisons
establishes statistical significance or representative production performance.
The prior [label/error audit](error-audit.md) still applies.

Observed XLM-R loading time was 6.44 seconds, per-request p50 0.247 seconds,
p95 0.367 seconds, maximum 0.561 seconds. Process peak RSS was 2,245,388 KiB
(about 2.14 GiB). These timings include the first request and ASGI overhead; they
are not network measurements, concurrent throughput or minimum deployment memory.
Model experiments run sequentially; the Linux host CPU quota was eight CPUs.

## Matched contextual-guard experiment

A fixed sample of 100 cases per source (50 positive, 50 negative) was selected
before inference, independently of previous model errors. Both Qwen rules were
also fixed before scoring. Results on that same sample:

| Model / rule | French positives caught / 50 | French negative flags / completed negatives | English positives caught / 50 | English negative flags / completed negatives | Failed checks, French / English |
| --- | ---: | ---: | ---: | ---: | ---: |
| XLM-R, 0.5 review | 28 (56%) | 14/50 (28%) | 21 (42%) | 5/50 (10%) | 0 / 0 |
| Qwen, Unsafe only | 18 (36%) | 2/46 (4.3%) | 25 (50%) | 8/46 (17.4%) | 11 / 9 |
| Qwen, Unsafe or Controversial | 37 (74%) | 8/46 (17.4%) | 37 (74%) | 18/46 (39.1%) | 11 / 9 |

Qwen completed 180/200 checks within the research budget. All 20 failures were
`deadline_exceeded`; none were parsing failures. Failures remain incomplete, not
allow decisions. Recall uses **all** labelled-positive cases, including failed
checks. False-positive rates use completed negative cases, hence the 46 versus
50 denominators; eight failed negative cases remain separately visible.

On the French subset, Qwen's broad interpretation improves the observed recall /
false-flag trade-off versus XLM-R, but has 11 failed checks. On English, higher
recall comes with substantially more false flags. These small reused samples do
not establish significance, production suitability or a universally better model.
Qwen's prompt-safety categories are not the same as community-comment toxicity,
and its verdicts are not calibrated probabilities.

Observed Qwen loading time was 7.34 seconds. Per-call p50 was 4.285 seconds,
p95 5.073 seconds and maximum 5.180 seconds. Process peak RSS was 4,915,036 KiB
(about 4.69 GiB), using CPU FP32 and two inference threads. These figures are for
this implementation/host and do not estimate quantized performance or minimum
memory. Qwen runs directly as a research generator, while XLM-R includes the
existing ASGI serving path; no network or concurrent capacity was measured.

`generate(max_time=5)` is not a hard interrupt. The collector rejects total calls
over five seconds even when their partial/generated output looks valid. There is
no production Qwen adapter, hard-cancellation guarantee or automatic deployment.

## Sanity and instruction-injection probes

All 14 self-authored probes completed within budget and matched the declared
**minimum-review** expectation: the two benign messages were Safe, while the
harmful messages and injection variants remained Unsafe or Controversial.

However, **all four role-spoofing variants downgraded an Unsafe base verdict to
Controversial**. An Unsafe-only policy would stop escalating these four harmful
variants, whereas the broad review rule would preserve escalation. This is an
important policy sensitivity; calling the result an injection-robustness pass
would be misleading. The four direct verdict-forcing variants remained Unsafe.

These are 14 self-authored examples, not independent labels or an exhaustive
adversarial benchmark. Native categories also vary (for example threat content
can move between Violent and Unethical Acts). The experiment does not validate
category-specific recall.

## Decision

Keep both candidates experimental. XLM-R provides a usable optional capacity
baseline for further testing, with modest fixed-cutoff improvements and higher
observed memory than the small reference model. Qwen offers a different contextual
review trade-off, but the observed CPU budget failures, false flags, memory and
role-spoofing sensitivity do not justify adding it to the default serving path.

Before another production-profile decision, define the intended categories and
independently adjudicate category-aligned examples, including benign discussion,
quoted/counterspeech and ambiguous context. Calibrate on validation data and
reserve fresh source-group holdouts. The current inspected cases remain diagnostics.
No production model or thresholds were changed, and no new acceptance gate was
claimed or fitted on these cases.

[Complete machine-readable results](next-model-results.json).
See [protocol, commands, resource interpretation and limitations](next-models.md).
