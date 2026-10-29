# Recorded text model comparison

Both candidates were tested on the same 1,500 examples: 50 toxic and 50 non-toxic
per language. At a fixed 0.5 cutoff:

| Candidate | Toxic escalated / 750 | Toxic allowed | Non-toxic escalated / 750 | Failed |
| --- | ---: | ---: | ---: | ---: |
| Horizon Small | 316 | 433 | 39 | 3 |
| Gravitee DistilBERT | 626 | 123 | 118 | 3 |

Escalated means review or block, not automatic blocking. All three failures per
candidate were `input_too_long`; failures remain separate from clean decisions.

Gravitee increases recall at this cutoff but escalates more benign text. In Amharic,
it escalated 34/50 non-toxic examples, compared with 6/50 for Horizon. These are
different score distributions, not interchangeable calibrated probabilities.

Recommendation: keep the replacement optional. Do not automatically replace the
reference model/policy. Calibrate on separate representative validation data and
evaluate on a new held-out set before recommending production thresholds. Broader
independent photo validation also remains outstanding.

The preparation excluded 232 normalized training/test overlaps and one conflicting
label group, and collapsed 15 duplicate test rows. This remains an in-domain publisher
test comparison, not independent proof of general moderation quality.

| Language | Horizon: toxic caught / 50 | Gravitee: toxic caught / 50 | Horizon: benign escalated / 50 | Gravitee: benign escalated / 50 |
| --- | ---: | ---: | ---: | ---: |
| am | 3 | 39 | 6 | 34 |
| ar | 28 | 42 | 6 | 16 |
| de | 13 | 39 | 0 | 2 |
| en | 46 | 48 | 1 | 2 |
| es | 22 | 40 | 2 | 2 |
| fr | 44 | 47 | 2 | 3 |
| he | 24 | 29 | 4 | 8 |
| hi | 10 | 47 | 0 | 2 |
| hin | 8 | 35 | 3 | 16 |
| it | 36 | 46 | 6 | 13 |
| ja | 9 | 44 | 0 | 5 |
| ru | 31 | 45 | 2 | 0 |
| tt | 26 | 49 | 6 | 4 |
| uk | 7 | 45 | 1 | 3 |
| zh | 9 | 31 | 0 | 8 |

These counts were recovered from the completed local-run output retained in this
conversation. Workspace maintenance removed the original local prediction files.
The [machine-readable record](recorded-results.json) preserves this distinction;
fresh workflow runs generate new reports and raw predictions. See
[reproduction and limitations](README.md). No model weights or dataset texts are
included in this repository.

## Ranking versus cutoff

The saved, fingerprint-verified predictions also support a ranking diagnostic.
ROC AUC measures whether toxic examples tend to score above benign examples;
0.5 is chance-like ranking and 1 is perfect ranking on the evaluated sample.
It is not accuracy at the current cutoff or a calibrated safety probability.

| Slice | Horizon AUC | Gravitee AUC |
| --- | ---: | ---: |
| Mean across all 15 languages | 0.8351 | 0.9036 |
| English | 0.9904 | 0.9924 |
| French | 0.9920 | 0.9856 |
| Hindi | 0.9104 | 0.9872 |
| Amharic | 0.5844 | 0.6176 |

French ranking is strong for both candidates; the missed toxic examples at 0.5
should not be interpreted as uniformly poor discrimination. Hindi also illustrates
why ranking quality and a fixed operating point differ. For Amharic, both models
have weak ranking on this sample, so moving the cutoff alone cannot provide strong
separation. AUC does not determine which operating point meets an application's
false-positive budget. These small samples do not establish significant differences
between close values or predict deployment performance.

[Complete per-language diagnostics](ranking-results.json) retain completed-class
counts and failures. The macro score is undefined if any language lacks a completed
class. Three length-limit failures per model remain excluded from AUC and visible
in those counts. No new cutoff was fitted, and these reused test cases are not
independent calibration data.
