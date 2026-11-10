# Photo evaluation

Check a policy on your own labelled photos before it acts on real content. The
harness submits each photo to a running Modera API and reports where the decision
differs from your label. It does not run models itself, choose thresholds or
install defaults.

## Cases

`cases.json` is a JSON array. Photos are files beside it.

```json
[
  {"id": "p1", "file": "p1.jpg", "expected": "allow", "slice": "ordinary"},
  {"id": "p2", "file": "p2.jpg", "expected": "block", "slice": "explicit"}
]
```

`expected` is `allow`, `review` or `block`. `slice` is optional and groups results
(for example by lighting, clothing or source). Define what each label means in plain
words before labelling, label before viewing model scores, and keep photos used to
tune thresholds apart from the photos you assess with.

## Run

```sh
export MODERA_API_KEY=...        # never pass the key as a flag
uv run --locked python -m evaluation.photos \
  --cases evaluation/data/cases.json \
  --output evaluation/results/run-1.json \
  --url http://127.0.0.1:8000 --policy default
```

HTTP is accepted only for loopback; use HTTPS otherwise. The output file must not
exist. `evaluation/data/` and `evaluation/results/` are ignored by Git.

## Read the report

- **missed**: expected `review` or `block`, but the decision was `allow`.
- **false_flags**: expected `allow`, but the decision was `review` or `block`.
- **wrong_level**: flagged, but as `review` where `block` was expected or the reverse.
- **failed_checks / failure_codes**: no decision was produced (for example a timeout
  or `check_unavailable`). These are counted apart and are never treated as `allow`.
  Re-run to retry; the harness does not retry for you.
- **provenance / provenance_consistent**: the policy ID and version, model revisions
  and preprocessing seen across the run. `false` means the configuration changed
  mid-run and the results should not be compared.
- **slices**: the same counts per slice. **total_seconds** and **slowest_seconds**
  describe the sequential run, not capacity.

Lists contain case IDs only, never photo content. Report counts alongside rates:
small or correlated samples give weak evidence. After changing thresholds, assess
again on fresh held-out photos. To compare configurations, run the harness once per
configuration against the same cases and compare the reports.
