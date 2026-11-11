# Evaluation

Two tools: one measures text accuracy on public datasets, and one checks a policy
on your own labelled photos.

## Text accuracy

`evaluation/text.py` scores pinned, human-labelled public datasets with pinned models
through Modera's real serving path, and writes [the accuracy page](../docs/accuracy.md).
Cutoffs are chosen on a validation half and every reported figure is from the held-out
half. The 90% recall / 5% false-flag target is fixed in the code before scoring.

```sh
uv sync --locked --extra inference
uv run --locked --extra inference python -m evaluation.text score \
  --cache model-cache --scores evaluation/reports/text-scores.json
uv run --locked --extra inference python -m evaluation.text report \
  --scores evaluation/reports/text-scores.json \
  --json evaluation/reports/text.json --markdown docs/accuracy.md
```

`score` downloads the models and datasets (several GB, see the model list in the page),
runs them, and saves raw scores with no text in them. It resumes if interrupted.
`report` recomputes every table from those scores in seconds, so the statistics can be
checked or extended without rerunning the models.

## Cutoffs for your own text

`evaluation/cutoffs.py` turns your labelled sample into a policy. The study above shows
that good cutoffs differ by model, and for some models by dataset: across six models and
two datasets they ranged from 0.35 to 0.998. A cutoff from someone else's data is a guess.

```sh
uv run --locked --extra inference python -m evaluation.cutoffs \
  --model-file models.json --data sample.jsonl \
  --policy-out policy.json --report-out report.md
```

- `--model-file` is a Modera [model file](../docs/models.md) with a `text` model.
- The sample is `.jsonl` (`{"text": "...", "toxic": true}` per line) or `.csv` with `text`
  and `toxic` columns. Exact duplicates are collapsed and conflicting labels dropped.
- Each half of the sample needs at least 50 toxic and 50 clean examples, or the command
  refuses, because a 5% false-flag rate cannot be measured on fewer.
- Cutoffs are chosen on one half, picked by a hash of the text. The report's figures all
  come from the other half, with 95% intervals and the review load at 1%, 5% and 20% toxic
  traffic.
- `policy.json` has a review cutoff that holds false flags to 5% and, when it can be told
  apart, a block cutoff that holds them to 1%. It is checked against Modera's policy
  schema before it is written, and it will not overwrite an existing file.
- It says whether the 90% recall / 5% false-flag target was met. It refuses outright if no
  useful cutoff exists, since a policy that never flags anything would mislead.

On the French study data this reproduces the study's reference-model numbers exactly.

## Photo evaluation

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
