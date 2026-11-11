"""Choose review and block cutoffs for a text policy from your own labelled sample.

Cutoffs are chosen on one half of the sample, picked by a hash of the text, and checked
on the other half, which they never saw. The reported rates are therefore not tuned on
the data they describe. The policy written is validated against Modera's own policy
schema.
"""

import argparse
import csv
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evaluation.text import (
    BLOCK_FPR,
    MAX_FPR,
    MIN_RECALL,
    PREVALENCES,
    Row,
    at_cutoff,
    auc,
    bands,
    bootstrap_auc,
    cutoff_for,
    deduplicate,
    pct,
    prevalence_table,
    score_text,
    split_of,
)
from modera.model_config import TextModelSettings, load_models
from modera.policies import PolicyConfig

MIN_PER_CLASS = 50
TRUE = {"true", "1", "yes"}
FALSE = {"false", "0", "no"}

Scorer = Callable[[TextModelSettings, list[Row]], list[float | None]]


class CutoffError(Exception):
    """The sample cannot support a trustworthy choice of cutoffs."""


def parse_label(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE:
        return True
    if text in FALSE:
        return False
    raise CutoffError(f"unrecognised label {value!r}; use true or false")


def load_sample(path: Path) -> tuple[list[Row], dict[str, int], bytes]:
    """Read JSON Lines or CSV with a `text` and a `toxic` field."""
    data = path.read_bytes()
    if path.suffix == ".jsonl":
        records = [
            json.loads(line) for line in data.decode().splitlines() if line.strip()
        ]
    elif path.suffix == ".csv":
        records = list(csv.DictReader(data.decode("utf-8-sig").splitlines()))
    else:
        raise CutoffError("the sample must be a .jsonl or .csv file")
    pairs = []
    for record in records:
        if "text" not in record or "toxic" not in record:
            raise CutoffError("every record needs a `text` and a `toxic` field")
        if str(record["text"]).strip():
            pairs.append((str(record["text"]), parse_label(record["toxic"])))
    kept, counts = deduplicate(pairs)
    counts["records"] = len(pairs)
    return (
        [Row(text, positive, split_of(text)) for text, positive in kept],
        counts,
        data,
    )


def require_enough(groups: dict[str, tuple[list[float], list[float]]]) -> None:
    short = [
        f"{split} has {len(toxic)} toxic and {len(clean)} clean"
        for split, (toxic, clean) in groups.items()
        if min(len(toxic), len(clean)) < MIN_PER_CLASS
    ]
    if short:
        raise CutoffError(
            f"each half needs at least {MIN_PER_CLASS} toxic and {MIN_PER_CLASS} clean "
            f"examples to measure a {MAX_FPR:.0%} false-flag rate; " + "; ".join(short)
        )


def choose(
    rows: list[Row],
    scores: list[float | None],
    category: str,
    policy_id: str,
    digest: str,
) -> dict[str, Any]:
    groups: dict[str, tuple[list[float], list[float]]] = {
        "validation": ([], []),
        "test": ([], []),
    }
    for row, score in zip(rows, scores, strict=True):
        if score is not None:
            groups[row.split][0 if row.positive else 1].append(score)
    require_enough(groups)
    (val_pos, val_neg), (test_pos, test_neg) = groups["validation"], groups["test"]
    review = cutoff_for(val_pos, val_neg, MAX_FPR)
    on_validation = at_cutoff(val_pos, val_neg, review)
    if review > 1.0 or on_validation["recall"]["count"] == 0:
        raise CutoffError(
            f"no useful cutoff holds false flags to {MAX_FPR:.0%} on this sample: "
            "any cutoff within that budget would flag no toxic example at all, because "
            "the model scores clean text as high as toxic text"
        )
    block = cutoff_for(val_pos, val_neg, BLOCK_FPR)
    rule: dict[str, Any] = {"category": category, "review_threshold": review}
    if review < block <= 1.0:
        rule["block_threshold"] = block
    policy = {
        "policies": [
            {
                "id": policy_id,
                "version": f"calibrated-{digest[:8]}",
                "text": [rule],
            }
        ]
    }
    PolicyConfig.model_validate_json(json.dumps(policy))
    block_edge = rule.get("block_threshold", float("inf"))
    held_out = at_cutoff(test_pos, test_neg, review)
    recall, false_flags = (
        held_out["recall"]["value"],
        held_out["false_positive_rate"]["value"],
    )
    met = (
        on_validation["recall"]["value"] >= MIN_RECALL
        and recall >= MIN_RECALL
        and false_flags <= MAX_FPR
    )
    return {
        "policy": policy,
        "failed_requests": sum(score is None for score in scores),
        "examples": {
            split: {"toxic": len(p), "clean": len(n)}
            for split, (p, n) in groups.items()
        },
        "validation_at_review_cutoff": on_validation,
        "test_at_review_cutoff": held_out,
        "auc_test": {
            "value": round(auc(test_pos, test_neg), 4),
            "ci95": bootstrap_auc(test_pos, test_neg),
        },
        "test_bands": bands(test_pos, test_neg, review, block_edge),
        "prevalence": prevalence_table(recall, false_flags),
        "target_met": met,
    }


def render(result: dict[str, Any], counts: dict[str, int], model: str) -> str:
    rule = result["policy"]["policies"][0]["text"][0]
    held, val = result["test_at_review_cutoff"], result["validation_at_review_cutoff"]
    low, high = result["auc_test"]["ci95"]
    out = [
        "# Cutoffs for your sample",
        "",
        f"Model `{model}`. {counts['records']} labelled examples; "
        f"{counts['duplicates_collapsed']} duplicates collapsed, "
        f"{counts['conflicting_rows_dropped']} conflicting rows dropped, "
        f"{result['failed_requests']} requests failed. Cutoffs were chosen on "
        "one half of the sample and every figure below comes from the other half.",
        "",
        "## Policy rule",
        "",
        f"- Review at a score of {rule['review_threshold']:.4f} or more, the lowest "
        "cutoff "
        f"that holds false flags to {MAX_FPR:.0%} on the first half.",
    ]
    if "block_threshold" in rule:
        out.append(
            f"- Block at {rule['block_threshold']:.4f} or more (false flags held to "
            f"{BLOCK_FPR:.0%})."
        )
    else:
        out.append(
            "- No block cutoff: it could not be separated from the review cutoff."
        )
    t = result["examples"]["test"]
    out += [
        "",
        "## Held-out result",
        "",
        f"On {t['toxic']} toxic and {t['clean']} clean examples the review cutoff "
        f"caught {pct(held['recall'])} of toxic text and flagged "
        f"{pct(held['false_positive_rate'])} of clean text. "
        f"ROC AUC {result['auc_test']['value']:.3f} "
        f"({low:.3f}-{high:.3f}). On the half it was chosen on it caught "
        f"{pct(val['recall'])}.",
        "",
        f"**Target** ({MIN_RECALL:.0%} caught, at most {MAX_FPR:.0%} flagged): "
        f"{'met' if result['target_met'] else 'not met'}.",
        "",
        "## What it means when toxic text is rare",
        "",
        "| Toxic share of traffic | Flagged per 1,000 | Toxic among flagged | "
        "Toxic missed per 1,000 |",
        "| --- | --- | --- | --- |",
    ]
    for item in result["prevalence"]:
        share = item["toxic_among_flagged"]
        out.append(
            f"| {item['prevalence']:.0%} | {item['flagged_per_1000']:.0f} | "
            f"{'n/a' if share is None else f'{share:.0%}'} | "
            f"{item['toxic_missed_per_1000']:.0f} |"
        )
    out += [
        "",
        "## Before you rely on it",
        "",
        "- The shares above assume "
        f"{', '.join(f'{p:.0%}' for p in PREVALENCES)} toxic traffic. "
        "Measure your own.",
        "- Rerun this whenever the model, the policy or the kind of content changes. "
        "Cutoffs do not transfer between models or audiences.",
        "- A sample this size gives wide intervals. The ranges above are the honest "
        "uncertainty, so do not read the point values as exact.",
        "- Scores are not probabilities. The cutoffs are positions in a ranking.",
        "",
    ]
    return "\n".join(out)


def run(
    model_file: Path,
    data: Path,
    policy_out: Path,
    report_out: Path,
    policy_id: str = "default",
    scorer: Scorer = score_text,
) -> dict[str, Any]:
    text_model = load_models(model_file).text
    if text_model is None:
        raise CutoffError("the model file has no `text` model")
    category = next(iter(text_model.label_map.values()))
    rows, counts, raw = load_sample(data)
    result = choose(
        rows,
        scorer(text_model, rows),
        category,
        policy_id,
        hashlib.sha256(raw).hexdigest(),
    )
    with policy_out.open("x") as stream:
        stream.write(json.dumps(result["policy"], indent=2) + "\n")
    with report_out.open("x") as stream:
        stream.write(render(result, counts, text_model.id))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-file", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--policy-out", type=Path, required=True)
    parser.add_argument("--report-out", type=Path, required=True)
    parser.add_argument("--policy-id", default="default")
    args = parser.parse_args()
    try:
        result = run(
            args.model_file, args.data, args.policy_out, args.report_out, args.policy_id
        )
    except CutoffError as error:
        parser.exit(2, f"error: {error}\n")
    verdict = "met" if result["target_met"] else "not met"
    print(f"wrote {args.policy_out} and {args.report_out}; target {verdict}")


if __name__ == "__main__":
    main()
