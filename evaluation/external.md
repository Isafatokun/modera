# External-source evaluation

These commands evaluate the pinned text candidates on two sources beyond the
existing TextDetox diagnostic. They exercise Modera's actual serving engine through
in-process ASGI HTTP; they do not measure network latency or capacity.

```sh
uv sync --locked --extra inference --group evaluation --group benchmark
uv run --locked --extra inference python -m evaluation.compare_text models --cache model-cache
uv run --locked --extra inference python -m evaluation.external_text prepare \
  --directory evaluation/data/external-v1 --cache model-cache
for candidate in horizon-small gravitee-distilbert; do
  uv run --locked --extra inference python -m evaluation.external_text run \
    --directory evaluation/data/external-v1 --cache model-cache \
    --candidate "evaluation/candidates/$candidate.json" \
    --output "evaluation/results/external-v1/$candidate"
done
```

Preparation pins ToxiFrench and the human-annotated ToxiGen test subset. French
`oui` is positive; English uses the declared mapping `toxicity_human >= 3` on
its 1–5 scale, not the generator's intent or an asserted universal label cutoff.
ToxiFrench uses native comments and a semi-automated annotation pipeline with
human verification. ToxiGen uses generated text with human ratings. Keep their
metrics separate.

The cohort removes normalized exact duplicates and conflicting labels, and excludes
inputs over 4,096 characters or either pinned tokenizer's configured limit **before
inference**. Exclusion counts are reported. Results therefore describe the common
supported-length cohort, not arbitrary incoming requests. No truncation or retries
are used. The 5-second serving deadline remains unchanged.

A salted normalized-text hash reserves approximately half the examples for testing.
This prevents exact duplicates crossing the split, but cannot establish thread,
author, prompt-family, translation, near-duplicate or training independence. These
are external-source diagnostics, not a representative application dataset.

The exploratory review gate is fixed before scoring: at most 5% validation false
positives and at least 90% validation recall, separately per source. This is a
research target, not an agreed deployment requirement. All validation requests must
succeed. The collector writes every source's proposal (or `no_candidate` /
`incomplete`) before reading held-out labels and scoring test cases. A failed gate
never falls back to a weaker target. Assessment uses the frozen proposal without
retuning. Fixed reference-cutoff diagnostics at 0.5 are reported even if no new
cutoff qualifies.

Per-source manifests and response files allow independent reproduction with the
existing assessment command:

```sh
uv run --locked python -m evaluation.assess \
  --proposal evaluation/results/external-v1/horizon-small/toxifrench-proposal.json \
  --cases evaluation/results/external-v1/horizon-small/toxifrench-cases.json \
  --validation-results evaluation/results/external-v1/horizon-small/toxifrench-validation-results.jsonl \
  --labels evaluation/results/external-v1/horizon-small/toxifrench-test-labels.json \
  --test-results evaluation/results/external-v1/horizon-small/toxifrench-test-results.jsonl \
  --output evaluation/results/external-v1/horizon-small/reproduced-assessment.json
```

This assessment command requires a `proposal`, rather than `no_candidate` or
`incomplete`. Existing output paths are rejected. Do not reuse these inspected
held-out cases after changing thresholds. No production policy is installed.

## Image coverage challenge

The public Multimodal Pragmatic Jailbreak repository provides human-labelled
safe/unsafe images. The pinned manifest restricts evaluation to source IDs 92–133,
the sexual-safety category: 42 safe/unsafe pairs, before duplicate checks.

```sh
uv run --locked python - <<'PYTHON'
import json
from pathlib import Path
model = json.loads(Path("deploy/reference/models.json").read_text())["image"]
model["cache_dir"] = str(Path("model-cache/hub").resolve())
config = Path("evaluation/data/photo-models.json")
config.parent.mkdir(parents=True, exist_ok=True)
config.write_text(json.dumps({"image": model}))
PYTHON
uv run --locked --extra inference python -m modera.prepare_models \
  --config evaluation/data/photo-models.json
uv run --locked --extra inference python -m evaluation.photo_challenge prepare \
  --directory evaluation/data/photo-challenge-v1
uv run --locked --extra inference python -m evaluation.photo_challenge run \
  --directory evaluation/data/photo-challenge-v1 --cache model-cache \
  --output evaluation/results/photo-challenge-v1
```

Downloads verify exact Git blob IDs from the pinned source commit. Image content
is SHA-256 checked before requests. Exact same-label duplicates are collapsed;
conflicting-label duplicates are excluded and listed. The current reference photo
policy is frozen during preparation, including the 0.95 porn/hentai blocking
cutoffs. There is no threshold selection.

This is a **coverage diagnostic**, not a natural-photo nudity accuracy benchmark:
sexual harm can depend on rendered text and context, which the selected nudity
classifier does not model. Broad sexual-safety labels are not verified ground truth
for its porn/hentai outputs. Do not use these rates to choose nudity cutoffs.
The small generated-image corpus also does not establish training independence.

UnsafeBench has broader human image annotations, but its image files require
account access and acceptance of the publisher's data-use conditions. That
benchmark was not downloaded. Broad independently labelled photo validation,
including explicit nudity, suggestive clothing, artwork and benign people, remains
necessary before recommending a deployment policy. The public challenge has no
top-level dataset licence; raw images are not redistributed with Modera.

Sources:

- ToxiFrench: https://huggingface.co/datasets/AxelDlv00/ToxiFrench
- ToxiFrench paper: https://aclanthology.org/2026.findings-acl.1074/
- ToxiGen: https://huggingface.co/datasets/toxigen/toxigen-data
- ToxiGen authors: https://github.com/microsoft/TOXIGEN
- Image challenge: https://github.com/multimodalpragmatic/multimodalpragmatic
- UnsafeBench: https://huggingface.co/datasets/yiting/UnsafeBench
