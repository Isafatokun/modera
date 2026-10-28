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
