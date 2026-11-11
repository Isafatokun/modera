# Text accuracy

Measured with `python -m evaluation.text`. Thresholds are chosen on a validation half and every figure below comes from the held-out half. Ranges are 95% confidence intervals. The two datasets are never pooled.

**Target.** At most 5% of clean examples flagged and at least 90% of toxic ones caught, fixed before scoring. A model meets it only if the cutoff that holds false positives to 5% on validation also reaches 90% recall there.

The main comparison and the bands were fixed before scoring. The calibration, prevalence and two-model sections were added after seeing the first results, so treat them as exploratory.

## Result

- **ToxiFrench (French):** none of the models met the target.
- **ToxiGen (English):** none of the models met the target.

## Models

| Model | Task | Licence | Revision |
| --- | --- | --- | --- |
| Horizon Small (`Horizon-Labs/multilingual-toxicity-small`) | toxicity | Apache-2.0 | `01adcc628a33` |
| Horizon Base (`Horizon-Labs/multilingual-toxicity-base`) | toxicity | Apache-2.0 | `3ed527edbce0` |
| Gravitee DistilBERT (`gravitee-io/distilbert-multilingual-toxicity-classifier`) | toxicity | OpenRAIL++ | `865ad1b453a4` |
| TextDetox BERT (`textdetox/bert-multilingual-toxicity-classifier`) | toxicity | OpenRAIL++ | `8fe5526e9c6b` |
| TextDetox XLM-R Large v2 (`textdetox/xlmr-large-toxicity-classifier-v2`) | toxicity | OpenRAIL++ | `38ab1b77bd3f` |
| Horizon hate speech (`Horizon-Labs/multilingual-hate-speech-base`) | hate speech | Apache-2.0 | `9ee208ff8052` |

Not evaluated: `malexandersalazar/xlm-roberta-large-binary-cls-toxicity`, because its model card lists toxigen/toxigen-data as training data, so the English result would not be a held-out test.

## ToxiFrench (French)

Dataset `AxelDlv00/ToxiFrench` at `6280dba8a77f`; toxic means label is 'oui'. 1388 rows, 0 duplicates collapsed, 0 conflicting rows dropped, 0 over the length limit excluded.

| Model | Held-out toxic / clean | ROC AUC | Recall @0.5 | False flags @0.5 | Recall @ validation cutoff | False flags @ validation cutoff | Meets target |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Horizon Small | 347 / 328 | 0.821 (0.789-0.854) | 39.5% (34.5%-44.7%) | 9.2% (6.5%-12.8%) | 27.7% (23.2%-32.6%) | 4.6% (2.8%-7.4%) | no |
| Horizon Base | 347 / 328 | 0.831 (0.800-0.866) | 30.0% (25.4%-35.0%) | 5.5% (3.5%-8.5%) | 24.5% (20.3%-29.3%) | 3.7% (2.1%-6.3%) | no |
| Gravitee DistilBERT | 347 / 328 | 0.712 (0.674-0.751) | 64.0% (58.8%-68.8%) | 31.7% (26.9%-36.9%) | 22.5% (18.4%-27.2%) | 8.8% (6.2%-12.4%) | no |
| TextDetox BERT | 347 / 328 | 0.738 (0.701-0.777) | 51.6% (46.3%-56.8%) | 18.0% (14.2%-22.5%) | 25.1% (20.8%-29.9%) | 6.4% (4.2%-9.6%) | no |
| TextDetox XLM-R Large v2 | 347 / 328 | 0.748 (0.711-0.784) | 60.5% (55.3%-65.5%) | 20.7% (16.7%-25.4%) | 32.0% (27.3%-37.1%) | 4.9% (3.0%-7.8%) | no |
| Horizon hate speech | 347 / 328 | 0.829 (0.798-0.859) | 20.8% (16.8%-25.3%) | 2.7% (1.5%-5.1%) | 31.7% (27.0%-36.8%) | 4.0% (2.3%-6.7%) | no |

### Review and block bands

A two-threshold policy: review at the cutoff that holds validation false positives to 5%, block at the one that holds them to 1%. Counts are held-out examples.

| Model | Toxic: allow / review / block | Clean: allow / review / block |
| --- | --- | --- |
| Horizon Small | 251 / 59 / 37 | 313 / 11 / 4 |
| Horizon Base | 262 / 47 / 38 | 316 / 10 / 2 |
| Gravitee DistilBERT | 269 / 64 / 14 | 299 / 29 / 0 |
| TextDetox BERT | 260 / 75 / 12 | 307 / 21 / 0 |
| TextDetox XLM-R Large v2 | 236 / 95 / 16 | 312 / 16 / 0 |
| Horizon hate speech | 237 / 73 / 37 | 315 / 12 / 1 |

### Do the scores mean what they say?

A score of 0.7 is only useful as a probability if about 70% of examples scored near 0.7 are toxic. Brier score is the mean squared error of the score as a probability (lower is better; a constant prediction of the base rate scores the first figure below). Expected calibration error (ECE) is the average gap between score and observed rate. The calibrated columns map scores with isotonic regression fitted on the validation half. A monotone map cannot change ranking, so AUC and the recall at a given false-flag rate stay the same.

Base rate on the held-out half and Brier of a constant prediction: 0.250.

| Model | Brier raw | Brier calibrated | ECE raw | ECE calibrated |
| --- | --- | --- | --- | --- |
| Horizon Small | 0.220 | 0.175 | 0.206 | 0.059 |
| Horizon Base | 0.232 | 0.168 | 0.231 | 0.053 |
| Gravitee DistilBERT | 0.293 | 0.220 | 0.268 | 0.068 |
| TextDetox BERT | 0.301 | 0.210 | 0.284 | 0.053 |
| TextDetox XLM-R Large v2 | 0.287 | 0.212 | 0.277 | 0.091 |
| Horizon hate speech | 0.298 | 0.166 | 0.316 | 0.030 |

Reliability of the highest-AUC model (Horizon Base), raw scores, held-out half:

| Score range | Examples | Mean score | Actually toxic |
| --- | --- | --- | --- |
| 0.0-0.1 | 176 | 0.04 | 10% |
| 0.1-0.2 | 105 | 0.15 | 40% |
| 0.2-0.3 | 106 | 0.25 | 59% |
| 0.3-0.4 | 88 | 0.35 | 66% |
| 0.4-0.5 | 78 | 0.45 | 79% |
| 0.5-0.6 | 61 | 0.55 | 75% |
| 0.6-0.7 | 39 | 0.65 | 95% |
| 0.7-0.8 | 18 | 0.75 | 94% |
| 0.8-0.9 | 4 | 0.84 | 100% |

### What this means when toxic text is rare

These test sets are about half toxic. Real traffic rarely is. Holding each model's held-out recall and false-flag rate fixed at the 5%-budget cutoff, this is what a reviewer would see per 1,000 items at different shares of toxic content. The last figure in each cell is toxic items that slipped through.

| Model | 1% toxic | 5% toxic | 20% toxic |
| --- | --- | --- | --- |
| Horizon Small | 48 flagged, 6% toxic, 7 missed | 57 flagged, 24% toxic, 36 missed | 92 flagged, 60% toxic, 145 missed |
| Horizon Base | 39 flagged, 6% toxic, 8 missed | 47 flagged, 26% toxic, 38 missed | 78 flagged, 63% toxic, 151 missed |
| Gravitee DistilBERT | 90 flagged, 2% toxic, 8 missed | 95 flagged, 12% toxic, 39 missed | 116 flagged, 39% toxic, 155 missed |
| TextDetox BERT | 66 flagged, 4% toxic, 8 missed | 73 flagged, 17% toxic, 38 missed | 101 flagged, 49% toxic, 150 missed |
| TextDetox XLM-R Large v2 | 52 flagged, 6% toxic, 7 missed | 62 flagged, 26% toxic, 34 missed | 103 flagged, 62% toxic, 136 missed |
| Horizon hate speech | 42 flagged, 7% toxic, 7 missed | 54 flagged, 30% toxic, 34 missed | 95 flagged, 67% toxic, 137 missed |

### Does a second model help?

Flag an item if either Horizon Base or Horizon Small (the two best by validation AUC) crosses its own cutoff, each held to 2.5% false flags on validation. This is an experiment about what a multi-model text policy could give. The service serves one text model, so this is not a shipped feature.

| Rule | Recall | False flags |
| --- | --- | --- |
| Horizon Base alone, 5% budget | 24.5% (20.3%-29.3%) | 3.7% (2.1%-6.3%) |
| Horizon Small alone, 5% budget | 27.7% (23.2%-32.6%) | 4.6% (2.8%-7.4%) |
| Either of the two | 25.7% (21.3%-30.5%) | 3.4% (1.9%-5.9%) |

## ToxiGen (English)

Dataset `toxigen/toxigen-data` at `ea082a8973a2`; toxic means human toxicity rating >= 3 of 5. 940 rows, 0 duplicates collapsed, 0 conflicting rows dropped, 0 over the length limit excluded.

| Model | Held-out toxic / clean | ROC AUC | Recall @0.5 | False flags @0.5 | Recall @ validation cutoff | False flags @ validation cutoff | Meets target |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Horizon Small | 210 / 252 | 0.730 (0.686-0.771) | 26.7% (21.1%-33.0%) | 7.5% (4.9%-11.5%) | 27.1% (21.6%-33.5%) | 8.3% (5.5%-12.4%) | no |
| Horizon Base | 210 / 252 | 0.735 (0.692-0.776) | 19.5% (14.7%-25.4%) | 4.8% (2.7%-8.1%) | 30.0% (24.2%-36.5%) | 7.5% (4.9%-11.5%) | no |
| Gravitee DistilBERT | 210 / 252 | 0.699 (0.651-0.749) | 32.9% (26.9%-39.5%) | 12.7% (9.1%-17.4%) | 24.3% (19.0%-30.5%) | 6.3% (4.0%-10.1%) | no |
| TextDetox BERT | 210 / 252 | 0.674 (0.623-0.725) | 25.2% (19.8%-31.5%) | 5.6% (3.3%-9.1%) | 25.2% (19.8%-31.5%) | 5.6% (3.3%-9.1%) | no |
| TextDetox XLM-R Large v2 | 210 / 252 | 0.776 (0.734-0.817) | 39.5% (33.2%-46.3%) | 6.8% (4.2%-10.5%) | 40.5% (34.1%-47.2%) | 6.8% (4.2%-10.5%) | no |
| Horizon hate speech | 210 / 252 | 0.864 (0.829-0.896) | 47.1% (40.5%-53.9%) | 5.2% (3.0%-8.6%) | 48.1% (41.4%-54.8%) | 5.6% (3.3%-9.1%) | no |

### Review and block bands

A two-threshold policy: review at the cutoff that holds validation false positives to 5%, block at the one that holds them to 1%. Counts are held-out examples.

| Model | Toxic: allow / review / block | Clean: allow / review / block |
| --- | --- | --- |
| Horizon Small | 153 / 27 / 30 | 231 / 13 / 8 |
| Horizon Base | 147 / 29 / 34 | 233 / 10 / 9 |
| Gravitee DistilBERT | 159 / 33 / 18 | 236 / 13 / 3 |
| TextDetox BERT | 157 / 19 / 34 | 238 / 10 / 4 |
| TextDetox XLM-R Large v2 | 125 / 27 / 58 | 235 / 4 / 13 |
| Horizon hate speech | 109 / 56 / 45 | 238 / 12 / 2 |

### Do the scores mean what they say?

A score of 0.7 is only useful as a probability if about 70% of examples scored near 0.7 are toxic. Brier score is the mean squared error of the score as a probability (lower is better; a constant prediction of the base rate scores the first figure below). Expected calibration error (ECE) is the average gap between score and observed rate. The calibrated columns map scores with isotonic regression fitted on the validation half. A monotone map cannot change ranking, so AUC and the recall at a given false-flag rate stay the same.

Base rate on the held-out half and Brier of a constant prediction: 0.248.

| Model | Brier raw | Brier calibrated | ECE raw | ECE calibrated |
| --- | --- | --- | --- | --- |
| Horizon Small | 0.251 | 0.212 | 0.202 | 0.068 |
| Horizon Base | 0.262 | 0.209 | 0.225 | 0.078 |
| Gravitee DistilBERT | 0.326 | 0.223 | 0.315 | 0.058 |
| TextDetox BERT | 0.338 | 0.226 | 0.328 | 0.067 |
| TextDetox XLM-R Large v2 | 0.299 | 0.196 | 0.298 | 0.065 |
| Horizon hate speech | 0.192 | 0.152 | 0.172 | 0.051 |

Reliability of the highest-AUC model (Horizon hate speech), raw scores, held-out half:

| Score range | Examples | Mean score | Actually toxic |
| --- | --- | --- | --- |
| 0.0-0.1 | 202 | 0.03 | 15% |
| 0.1-0.2 | 65 | 0.14 | 46% |
| 0.2-0.3 | 33 | 0.24 | 58% |
| 0.3-0.4 | 29 | 0.35 | 48% |
| 0.4-0.5 | 21 | 0.44 | 86% |
| 0.5-0.6 | 16 | 0.55 | 81% |
| 0.6-0.7 | 18 | 0.65 | 89% |
| 0.7-0.8 | 25 | 0.76 | 80% |
| 0.8-0.9 | 19 | 0.86 | 89% |
| 0.9-1.0 | 34 | 0.95 | 97% |

### What this means when toxic text is rare

These test sets are about half toxic. Real traffic rarely is. Holding each model's held-out recall and false-flag rate fixed at the 5%-budget cutoff, this is what a reviewer would see per 1,000 items at different shares of toxic content. The last figure in each cell is toxic items that slipped through.

| Model | 1% toxic | 5% toxic | 20% toxic |
| --- | --- | --- | --- |
| Horizon Small | 85 flagged, 3% toxic, 7 missed | 93 flagged, 15% toxic, 36 missed | 121 flagged, 45% toxic, 146 missed |
| Horizon Base | 78 flagged, 4% toxic, 7 missed | 87 flagged, 17% toxic, 35 missed | 120 flagged, 50% toxic, 140 missed |
| Gravitee DistilBERT | 65 flagged, 4% toxic, 8 missed | 72 flagged, 17% toxic, 38 missed | 99 flagged, 49% toxic, 151 missed |
| TextDetox BERT | 58 flagged, 4% toxic, 8 missed | 65 flagged, 19% toxic, 37 missed | 95 flagged, 53% toxic, 150 missed |
| TextDetox XLM-R Large v2 | 71 flagged, 6% toxic, 6 missed | 84 flagged, 24% toxic, 30 missed | 135 flagged, 60% toxic, 119 missed |
| Horizon hate speech | 60 flagged, 8% toxic, 5 missed | 77 flagged, 31% toxic, 26 missed | 141 flagged, 68% toxic, 104 missed |

### Does a second model help?

Flag an item if either Horizon hate speech or TextDetox XLM-R Large v2 (the two best by validation AUC) crosses its own cutoff, each held to 2.5% false flags on validation. This is an experiment about what a multi-model text policy could give. The service serves one text model, so this is not a shipped feature.

| Rule | Recall | False flags |
| --- | --- | --- |
| Horizon hate speech alone, 5% budget | 48.1% (41.4%-54.8%) | 5.6% (3.3%-9.1%) |
| TextDetox XLM-R Large v2 alone, 5% budget | 40.5% (34.1%-47.2%) | 6.8% (4.2%-10.5%) |
| Either of the two | 53.8% (47.1%-60.4%) | 8.3% (5.5%-12.4%) |

## Limits

- External datasets, not your traffic. Rates here do not transfer to another audience, language mix or prevalence; these samples are balanced by construction, not representative.
- Labels come from the datasets' own annotation, which has its own error. ToxiFrench uses a semi-automated pipeline with human checks, and ToxiGen uses model-generated text with human ratings.
- Several models list the TextDetox multilingual toxicity data as training data, and the Horizon models list Civil Comments. No evaluated card names ToxiFrench or ToxiGen, but cards can be incomplete, so overlap with these test sets is not ruled out.
- The split is a hash of the text, not time or author. Near-duplicates, shared authors and overlap with a model's training data are not ruled out.
- Dataset and model terms are their own. Check them before reuse.
