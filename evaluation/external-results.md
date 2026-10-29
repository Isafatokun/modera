# External-source results — 29 October 2026

**Neither text candidate qualifies for a new production review cutoff.** All four
exploratory validation gates failed the predeclared combination of >=90% recall
and <=5% false positives. No replacement model or policy was installed.

The earlier TextDetox comparison did not establish general moderation quality.
These external sources expose substantially different operating characteristics.

## Text: untouched test cases at the existing 0.5 review cutoff

| Candidate | Source | Toxic caught | Dataset-negative escalated | Failed |
| --- | --- | ---: | ---: | ---: |
| Horizon Small | ToxiFrench (French) | 154/363 (42.4%) | 33/391 (8.4%) | 0 |
| Gravitee DistilBERT | ToxiFrench (French) | 246/363 (67.8%) | 112/391 (28.6%) | 0 |
| Horizon Small | ToxiGen (English) | 44/180 (24.4%) | 24/286 (8.4%) | 0 |
| Gravitee DistilBERT | ToxiGen (English) | 60/180 (33.3%) | 37/286 (12.9%) | 0 |

Escalation means review or block under the fixed text reference policy. The test
cutoff was not tuned. English positives use the declared mapping of the mean human
rating >=3 on a 1–5 scale. These datasets measure different content/annotation
regimes; do not pool rates or interpret them as production estimates.

Preparation froze 1,388 ToxiFrench and 940 ToxiGen cases before scoring. Neither
had exact duplicates, conflicting normalized labels, or common-token-limit
exclusions. Hash assignment reserved 634 French and 474 English validation cases;
754 French and 466 English cases remained for testing. Each candidate processed
all 2,328 inputs: 4,656 successful text requests in total, no failed checks.

## Validation gate

At the lowest cutoff respecting the 5% false-positive budget:

| Candidate | Source | Validation recall | False positives | Proposal |
| --- | --- | ---: | ---: | --- |
| Horizon Small | French | 94/331 (28.4%) | 15/303 (5.0%) | none |
| Gravitee DistilBERT | French | 64/331 (19.3%) | 15/303 (5.0%) | none |
| Horizon Small | English | 68/222 (30.6%) | 12/252 (4.8%) | none |
| Gravitee DistilBERT | English | 48/222 (21.6%) | 12/252 (4.8%) | none |

All fall below the 90% recall requirement. `no_candidate` emits no threshold or
policy rule. Because none qualified, there is no calibrated proposal to assess
on the reserved test set. The fixed-cutoff test table above is a diagnostic,
not a passed calibrated-policy assessment. Targets were not relaxed after results.

## Image: contextual sexual-safety coverage

The pinned public Multimodal Pragmatic Jailbreak corpus provided 42 safe and
42 unsafe generated images in its sexual-safety category. All 84 were unique
and all 84 requests completed. At the reference photo policy's unchanged 0.95
porn/hentai block cutoffs:

- Unsafe examples escalated: **1/42**; 41 were allowed.
- Safe examples escalated: **2/42**; 40 were allowed.

This shows the reference policy does not cover this broader challenge. It does
**not** estimate explicit-nudity accuracy: sexual harm can depend on rendered text
and context, while the selected model is a nudity classifier. The dataset's labels
are not verified porn/hentai labels, and no threshold was tuned on them.

UnsafeBench offers broader human image annotations, but its files require account
access and acceptance of the publisher's data-use terms. It was not downloaded.
Broad independently labelled photo validation remains incomplete. The public
challenge has no top-level dataset licence; raw images are not redistributed.

## Recommendation

Keep the reference setup explicitly experimental. Evaluate other pinned text
candidates on a new validation/held-out cohort; these now-inspected test cases
remain diagnostic only. Establish the application's category definitions and
false-positive/recall requirements before selecting an operating point.

For photos, obtain an appropriately licensed, independently labelled natural-photo
benchmark with labels aligned to explicit nudity, suggestive clothing, artwork and
benign people. Test broader contextual sexual harm separately. These results do
not support a production-quality default recommendation or a new release claim.

## Evidence and reproduction

See [commands, source links and limitations](external.md) and
[machine-readable results](external-results.json). Both models use their existing
pinned configurations. Full API results, proposals, labels/IDs and input
fingerprints are retained in the accompanying evidence archive; raw source texts,
images, weights and API keys are excluded.

The runner uses the actual serving engine via in-process ASGI HTTP, with the
unchanged 5-second inference deadline, FP32 adapters and 2 inference threads.
This measures classification behavior, not network latency or capacity.
Local checks: 484 tests passed; two Docker-only tests skipped; lint, formatting,
strict typing and package build passed.

ToxiFrench uses native French comments with semi-automated annotation and human
verification. ToxiGen uses generated English text with human ratings. Hash-based
exact deduplication cannot establish source-family, thread, author, paraphrase,
translation or pretraining independence. Passing this exploratory gate would
still not establish production performance. No model or policy defaults changed.

A subsequent [error audit](error-audit.md) reproduces 48 sampled scores and flags
annotation/definition disagreements. Dataset-negative examples are not all
independently verified benign. Original labels and counts remain unchanged.
