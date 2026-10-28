# Policy calibration

Propose one model category's **review** threshold using labelled validation
responses from Modera. Choose an empirical false-positive budget and minimum
recall for your application. The command reserves held-out source groups for a
later assessment; held-out labels and responses are deliberately not accepted.

Freeze content and label before scoring. Keep related authors, sessions, videos,
translations and transformations in the same `group`. Do not relabel the already
inspected benchmark as new validation data. The tool checks declared groups, not
hidden related sources or model training overlap.

The case manifest is a JSON array. Positive examples use `expected: block`, meaning
this category should be escalated; negatives use `allow`. Test entries omit labels:

```json
[
  {"id":"v1","group":"session-1","split":"validation","input_type":"text","slice":"en","expected":"block"},
  {"id":"v2","group":"session-2","split":"validation","input_type":"text","slice":"en","expected":"allow"},
  {"id":"t1","group":"session-3","split":"test","input_type":"text","slice":"en"}
]
```

Validation observations are JSONL: `{"id":"v1","result":{...complete API response...}}`.
Provide exactly one successful response per validation case, with distinct request
IDs, consistent model/preprocessing provenance and the selected model finding.
Missing, failed, fallback and rule-derived checks cannot be silently dropped or
calibrated as clean results. Declare content eligibility and exclusions before scoring.
Calibrate one modality/category at a time; image model categories work identically.

```sh
uv run --locked python -m evaluation.calibrate \
  --cases evaluation/data/application-cases.json \
  --validation-results evaluation/data/validation-results.jsonl \
  --category toxicity --max-false-positive-rate 0.05 --min-recall 0.90 \
  --output evaluation/results/review-proposal.json
```

The numbers above illustrate the interface, not recommended production targets.
Selection takes the lowest cutoff meeting the false-positive budget, maximizing
validation recall, and emits no rule if the recall target is missed. Tied scores
stay together, respecting the service's inclusive `score >= threshold` comparison.
Keep generated numeric precision when transferring the rule.

Output records model provenance, input fingerprints, class/group counts, validation
metrics and per-slice rates. Budgets apply to the combined validation set; inspect
language/domain slices separately. Rates describe this sample, not statistical or
production guarantees. Small or correlated samples can give optimistic results.

Exit 0 writes a proposal with `policy_rule`; exit 2 writes `no_candidate` with no
threshold or rule when the targets cannot both be met. Invalid data exits 1, and
existing output files are never overwritten. No inference is run and no server
configuration is changed. After freezing a proposal, assess it on the held-out set
and review the complete versioned policy before deployment. This does not calibrate
an entire multi-rule policy or choose an automatic-blocking threshold.
