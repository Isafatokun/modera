# Text accuracy

Measured with `python -m evaluation.text`. Thresholds are chosen on a validation half and every figure below comes from the held-out half. Ranges are 95% confidence intervals. The two datasets are never pooled.

**Target.** At most 5% of clean examples flagged and at least 90% of toxic ones caught, fixed before scoring. A model meets it only if the cutoff that holds false positives to 5% on validation also reaches 90% recall there.

## Result

- **ToxiFrench (French):** none of the models met the target.
- **ToxiGen (English):** none of the models met the target.

## ToxiFrench (French)

Dataset `AxelDlv00/ToxiFrench` at `6280dba8a77f`; toxic means label is 'oui'. 1388 rows, 0 duplicates collapsed, 0 conflicting rows dropped, 0 over the length limit excluded.

| Model | Held-out toxic / clean | ROC AUC | Recall @0.5 | False flags @0.5 | Recall @ validation cutoff | False flags @ validation cutoff | Meets target |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Horizon Small | 347 / 328 | 0.821 (0.789-0.854) | 39.5% (34.5%-44.7%) | 9.2% (6.5%-12.8%) | 27.7% (23.2%-32.6%) | 4.6% (2.8%-7.4%) | no |
| Gravitee DistilBERT | 347 / 328 | 0.712 (0.674-0.751) | 64.0% (58.8%-68.8%) | 31.7% (26.9%-36.9%) | 22.5% (18.4%-27.2%) | 8.8% (6.2%-12.4%) | no |

### Review and block bands

A two-threshold policy: review at the cutoff that holds validation false positives to 5%, block at the one that holds them to 1%. Counts are held-out examples.

| Model | Toxic: allow / review / block | Clean: allow / review / block |
| --- | --- | --- |
| Horizon Small | 251 / 59 / 37 | 313 / 11 / 4 |
| Gravitee DistilBERT | 269 / 64 / 14 | 299 / 29 / 0 |

## ToxiGen (English)

Dataset `toxigen/toxigen-data` at `ea082a8973a2`; toxic means human toxicity rating >= 3 of 5. 940 rows, 0 duplicates collapsed, 0 conflicting rows dropped, 0 over the length limit excluded.

| Model | Held-out toxic / clean | ROC AUC | Recall @0.5 | False flags @0.5 | Recall @ validation cutoff | False flags @ validation cutoff | Meets target |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Horizon Small | 210 / 252 | 0.730 (0.686-0.771) | 26.7% (21.1%-33.0%) | 7.5% (4.9%-11.5%) | 27.1% (21.6%-33.5%) | 8.3% (5.5%-12.4%) | no |
| Gravitee DistilBERT | 210 / 252 | 0.699 (0.651-0.749) | 32.9% (26.9%-39.5%) | 12.7% (9.1%-17.4%) | 24.3% (19.0%-30.5%) | 6.3% (4.0%-10.1%) | no |

### Review and block bands

A two-threshold policy: review at the cutoff that holds validation false positives to 5%, block at the one that holds them to 1%. Counts are held-out examples.

| Model | Toxic: allow / review / block | Clean: allow / review / block |
| --- | --- | --- |
| Horizon Small | 153 / 27 / 30 | 231 / 13 / 8 |
| Gravitee DistilBERT | 159 / 33 / 18 | 236 / 13 / 3 |

## Limits

- External datasets, not your traffic. Rates here do not transfer to another audience, language mix or prevalence; these samples are balanced by construction, not representative.
- Labels come from the datasets' own annotation, which has its own error. ToxiFrench uses a semi-automated pipeline with human checks, and ToxiGen uses model-generated text with human ratings.
- Deduplication is on normalised exact text only. Near-duplicates, shared authors and overlap with a model's training data are not ruled out.
- Dataset and model terms are their own. Check them before reuse.
