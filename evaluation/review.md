# Category review and sealed label workflow

This workflow produces reviewer packets, disagreement records and category-specific
calibration manifests. It does not produce human ground truth automatically.
Definitions in `taxonomy.json` are a proposed, versioned annotation taxonomy, **not
an installed serving policy**. Review their scope before collecting labels.

The eight draft categories distinguish text abuse, identity attacks, threats and
explicit sexual content, plus image nudity, sexual activity, clothing and contextual
sexual harm. Content presence is separate from the application's allow/review/block
policy; swimwear or educational discussion is not automatically a block decision.

## 1. Prepare source-grouped inputs

Use `examples/review-items.json` as a format example. It is explicitly synthetic
and diagnostic, with no completed human labels. Real intake records declare origin,
prior use, source note, item ID, source group, modality, slice and content/context.
Text and images are supported. Image paths must remain within the intake directory;
files are decoded/validated and snapshotted into hash-named assets.

```sh
uv run --locked python -m evaluation.review prepare \
  --source examples/review-items.json --taxonomy evaluation/taxonomy.json \
  --output evaluation/data/review-packet
for reviewer in reviewer-a reviewer-b; do
  uv run --locked python -m evaluation.review bundle \
    --packet evaluation/data/review-packet --reviewer "$reviewer" \
    --output "evaluation/data/$reviewer.zip"
done
```

Each ZIP includes only blinded inputs, assets, taxonomy and a fresh blank review
form. It excludes source IDs, groups, split metadata, previous labels, model scores
and the other reviewer's annotations. Bundles are regenerated blank even when the
operator has filled an original template. Input text/context itself can still
identify a public example; metadata blinding is not anonymization or proof that
reviewers have never seen the source.

Keep the operator packet's private files separate from the reviewer ZIPs. Source
IDs map to opaque case IDs. Exact text duplicates (NFKC/case/whitespace normalized)
and exact image duplicates must share a source group. Reserve whole source/thread/
author/template/translation families together. A salted group hash fixes validation
and test assignment before scoring. Hidden related sources and training overlap
cannot be inferred or verified by the tool.

## 2. Obtain independent reviews

Reviewers inspect `review-inputs.json` and assets, fill every category in `review.json`
with `present`, `absent` or `uncertain`, optionally add notes, set their distinct
reviewer ID and honestly declare origin. Origin starts null so an unfilled template
cannot accidentally count as a human submission.

Only declared human submissions enter finalization. Model, assisted-human or
synthetic submissions,
incomplete cases, missing categories, duplicate IDs and mismatched packet hashes
are rejected. Software checks and reviewer IDs do not verify identity, independence
or honesty. Do not relabel model output as independent human review.

## 3. Compare and adjudicate

```sh
uv run --locked python -m evaluation.review compare \
  --packet evaluation/data/review-packet \
  --first reviews/reviewer-a.json --second reviews/reviewer-b.json \
  --output evaluation/data/comparison
```

The comparison records every disagreed category and the two reviewers' notes.
A blank `adjudication-template.json` is provided. A distinct third human fills only
those disagreements, with a nonempty rationale; uncertainty remains a valid result.

```sh
uv run --locked python -m evaluation.review finalize \
  --packet evaluation/data/review-packet \
  --first reviews/reviewer-a.json --second reviews/reviewer-b.json \
  --comparison evaluation/data/comparison/comparison.json \
  --adjudication reviews/adjudication.json \
  --output evaluation/data/reviewed
```

Omit `--adjudication` when there are no disagreements. Finalization binds consumed
review/comparison hashes and preserves origins, reviewer IDs, adjudicator and label
provenance. Uncertainty is never converted to absent or safe. The resulting private
label file contains test labels and is operator/review material, not model-selection
input. Access separation is procedural, not cryptographic secrecy.

## 4. Export one category for validation

```sh
uv run --locked python -m evaluation.review export \
  --packet evaluation/data/review-packet --reviewed evaluation/data/reviewed \
  --category targeted_abuse --model-category insult \
  --mapping-note "Declared experimental mapping; verify insult score coverage against this definition" \
  --output evaluation/data/abuse-export
```

A taxonomy category is not automatically equivalent to a model output. The chosen
model must actually expose the mapped category, and its meaning must be reviewed.
The command records this explicit mapping and rationale. The example does not
validate `insult` semantics or imply that the current toxicity-only profile exposes
it. Configure a compatible model mapping before scoring.

Uncertain cases are excluded under this declared rule before inference, with counts
retained. Both splits and both label classes must remain. Exported `cases.json`
contains validation labels and reserved **unlabelled** test IDs. Its shape works with
`evaluation.calibrate`. Score only validation cases first, using the complete API
responses and existing calibration instructions. Inference failures cannot be
silently dropped. Synthetic or previously used/unknown data are marked diagnostic;
`unseen_declared` remains a source declaration requiring audit, not verified novelty.

## 5. Unseal only after a qualifying proposal

```sh
uv run --locked python -m evaluation.review unseal \
  --reviewed evaluation/data/reviewed --exported evaluation/data/abuse-export \
  --proposal evaluation/results/abuse-proposal.json \
  --validation-results evaluation/data/abuse-validation-results.jsonl \
  --output evaluation/data/abuse-test-labels.json
```

The proposal must contain a real qualifying rule and match the exported cases,
chosen model category and original validation response fingerprint. Validation
selection/provenance is recomputed without test labels to reject mismatched
proposals. `no_candidate` does not unlock the test export. The held-out labels can
then be assessed using the frozen proposal and `evaluation.assess`, without retuning.
All output paths reject overwriting. A user with file access can still inspect or
forge local artifacts; hashes are binding checks, not proof of unseen data.

Use fresh held-out source groups after changing the selected model, taxonomy,
threshold or policy. This workflow makes genuine review collectable and auditable;
it does not supply reviewers, establish training independence, or authorize a
production-quality claim by itself.
