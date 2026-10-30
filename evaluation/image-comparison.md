# Compare image policies

Does adding a detector catch more violations, and how many acceptable photos does
it flag? Compare three frozen configurations on the same held-out photos:
classifier alone, detector alone, and both together. This command compares recorded
API responses; it does not run inference, select thresholds or install defaults.

First define the task in plain words. Explicit nudity, revealing clothing and
contextual sexual harm need different labels. Have reviewers label eligible photos
before viewing model scores. Record consent, source terms and exclusions privately.
Keep related photos in one source group, reserve whole groups for testing, and
calibrate using separate validation groups. Freeze model artifacts, preprocessing
and policies before unsealing test labels. Existing inspected examples are diagnostic.

The cases file is a JSON array using the existing evaluation format:

```json
[
  {"id":"p1","group":"session-1","split":"test","input_type":"image","slice":"ordinary","expected":"allow"},
  {"id":"p2","group":"session-2","split":"test","input_type":"image","slice":"explicit","expected":"block"}
]
```

Here `block` means the photo needs escalation; review and block are both escalations.
The report also retains their separate actions. The two records illustrate the
format, not an adequate evaluation sample.

For each configuration, record one JSONL observation per case:
`{"id":"p1","result":{...complete API response...}}`, or
`{"id":"p2","error":"timeout"}`. Record failed checks instead of dropping them.
Use distinct requests for every run and case. Save the exact configuration files
with the study; response policy versions alone cannot prove thresholds were frozen.
Verify the files actually represent the named configurations: the comparator does
not infer adapter kinds from repository names or verify photo identity.

```sh
uv run --locked python -m evaluation.compare_images \
  --cases evaluation/data/photos/cases.json \
  --classifier evaluation/data/photos/classifier.jsonl \
  --detector evaluation/data/photos/detector.jsonl \
  --combined evaluation/data/photos/combined.jsonl \
  --output evaluation/results/image-comparison.json
```

Every run must contain exactly the same case IDs. Provenance must stay consistent
within each run; models and policies may differ between configurations. Fallback,
unsupported coverage and rules findings count as failed checks. Invalid responses
stop reporting. Outputs never overwrite existing files and contain no raw photos.
Input hashes bind the parsed records, not an assertion that the data were unseen.

Inspect misses, false alarms, failures, action precision/recall, source-group counts
and slices together. Paired decision changes include only cases where both runs
completed; they do not establish improvement on their own. Report counts with the
rates: small or correlated samples provide weak evidence. These are descriptive
sample results, with no statistical confidence or production guarantee. After
changing thresholds, use fresh held-out data. Measure latency and capacity separately.

No suitable independent labelled photo cohort has been established here yet. The
ONNX research candidate also has unresolved model licence metadata; see
[detector readiness](detector-readiness.md). No default is recommended by this guide.
