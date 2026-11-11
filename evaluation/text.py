"""Measure text moderation accuracy on pinned, human-labelled public datasets.

Thresholds are chosen on the validation half only. The test half is scored once with
those frozen thresholds, so reported numbers are not tuned on the data they describe.
"""

import argparse
import csv
import hashlib
import importlib
import json
import math
import random
import secrets
import tempfile
import unicodedata
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from modera.config import Settings
from modera.main import create_app
from modera.model_config import TextModelSettings

MAX_FPR = 0.05
MIN_RECALL = 0.90
BLOCK_FPR = 0.01
REFERENCE_CUTOFF = 0.5
SALT = "modera-text-v1"
MAX_CHARACTERS = 4096
BOOTSTRAPS = 1000
BINS = 10
PREVALENCES = (0.01, 0.05, 0.20)

SOURCES: dict[str, dict[str, str]] = {
    "ToxiFrench (French)": {
        "repo": "AxelDlv00/ToxiFrench",
        "revision": "6280dba8a77ff27ca866a91b1098a6f4f24934e3",
        "file": "data/test_dataset.csv",
        "delimiter": ";",
        "text": "content",
        "label": "literal_conclusion_annotator",
        "positive": "label is 'oui'",
    },
    "ToxiGen (English)": {
        "repo": "toxigen/toxigen-data",
        "revision": "ea082a8973a287097b17973343576e0c8da8eb3b",
        "file": "annotated_test.csv",
        "delimiter": ",",
        "text": "text",
        "label": "toxicity_human",
        "positive": "human toxicity rating >= 3 of 5",
    },
}
MODELS: dict[str, dict[str, Any]] = {
    "Horizon Small": {
        "id": "Horizon-Labs/multilingual-toxicity-small",
        "revision": "01adcc628a33848569816d4851a38544c321b92c",
        "activation": "sigmoid",
        "label_map": {"toxicity": "toxicity"},
    },
    "Horizon Base": {
        "id": "Horizon-Labs/multilingual-toxicity-base",
        "revision": "3ed527edbce089f6a9a6bff34151c88e281c97e6",
        "activation": "sigmoid",
        "label_map": {"toxicity": "toxicity"},
    },
    "Gravitee DistilBERT": {
        "id": "gravitee-io/distilbert-multilingual-toxicity-classifier",
        "revision": "865ad1b453a49e4a7f2471fc40b727ececd28314",
        "activation": "softmax",
        "label_map": {"toxic": "toxicity"},
    },
    "TextDetox BERT": {
        "id": "textdetox/bert-multilingual-toxicity-classifier",
        "revision": "8fe5526e9c6b69cf1a615104a2ad037fb266c670",
        "activation": "softmax",
        "label_map": {"LABEL_1": "toxicity"},
    },
    "TextDetox XLM-R Large v2": {
        "id": "textdetox/xlmr-large-toxicity-classifier-v2",
        "revision": "38ab1b77bd3ff7779777cfa5cda203d9912b14b9",
        "activation": "softmax",
        "label_map": {"toxic": "toxicity"},
    },
    "Horizon hate speech": {
        "id": "Horizon-Labs/multilingual-hate-speech-base",
        "revision": "9ee208ff8052f676bdd53fba82d90d8e4a0f9ead",
        "activation": "sigmoid",
        "label_map": {"hate": "toxicity"},
    },
}
MODEL_NOTES: dict[str, dict[str, str]] = {
    "Horizon Small": {"licence": "Apache-2.0", "task": "toxicity"},
    "Horizon Base": {"licence": "Apache-2.0", "task": "toxicity"},
    "Gravitee DistilBERT": {"licence": "OpenRAIL++", "task": "toxicity"},
    "TextDetox BERT": {"licence": "OpenRAIL++", "task": "toxicity"},
    "TextDetox XLM-R Large v2": {"licence": "OpenRAIL++", "task": "toxicity"},
    "Horizon hate speech": {"licence": "Apache-2.0", "task": "hate speech"},
}
EXCLUDED = {
    "malexandersalazar/xlm-roberta-large-binary-cls-toxicity": (
        "its model card lists toxigen/toxigen-data as training data, so the English "
        "result would not be a held-out test"
    ),
}


@dataclass(frozen=True)
class Row:
    text: str
    positive: bool
    split: str


def fingerprint(text: str) -> str:
    text = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return hashlib.sha256(text.encode()).hexdigest()


def split_of(text: str) -> str:
    digest = hashlib.sha256((SALT + fingerprint(text)).encode()).digest()
    return "validation" if digest[0] < 128 else "test"


def read_source(name: str, cache: Path) -> list[tuple[str, bool]]:
    spec = SOURCES[name]
    hub = importlib.import_module("huggingface_hub")
    path = hub.hf_hub_download(
        spec["repo"],
        spec["file"],
        revision=spec["revision"],
        repo_type="dataset",
        cache_dir=cache / "datasets",
    )
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream, delimiter=spec["delimiter"]):
            text, label = row[spec["text"]], row[spec["label"]]
            positive = (
                label == "oui" if name.startswith("ToxiFrench") else float(label) >= 3
            )
            if text.strip():
                rows.append((text, positive))
    return rows


def deduplicate(
    rows: list[tuple[str, bool]],
) -> tuple[list[tuple[str, bool]], dict[str, int]]:
    groups: dict[str, list[tuple[str, bool]]] = {}
    for text, positive in rows:
        groups.setdefault(fingerprint(text), []).append((text, positive))
    kept, counts = [], {"duplicates_collapsed": 0, "conflicting_rows_dropped": 0}
    for members in groups.values():
        if len({positive for _, positive in members}) != 1:
            counts["conflicting_rows_dropped"] += len(members)
            continue
        counts["duplicates_collapsed"] += len(members) - 1
        kept.append(min(members))
    return sorted(kept), counts


def token_counts(texts: list[str], cache: Path) -> list[int]:
    """Longest tokenised length of each text across every model being compared."""
    transformers = importlib.import_module("transformers")
    longest = [0] * len(texts)
    for model in MODELS.values():
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            model["id"],
            revision=model["revision"],
            cache_dir=cache / "hub",
            local_files_only=True,
        )
        for index, text in enumerate(texts):
            longest[index] = max(longest[index], len(tokenizer(text)["input_ids"]))
    return longest


def build_cohort(name: str, cache: Path) -> tuple[list[Row], dict[str, int]]:
    raw = read_source(name, cache)
    rows, counts = deduplicate(raw)
    lengths = token_counts([text for text, _ in rows], cache)
    kept = [
        Row(text, positive, split_of(text))
        for (text, positive), length in zip(rows, lengths, strict=True)
        if len(text) <= MAX_CHARACTERS and length <= 256
    ]
    counts.update(source_rows=len(raw), too_long_excluded=len(rows) - len(kept))
    return kept, counts


def score_model(model: str, rows: list[Row], cache: Path) -> list[float | None]:
    spec = {**MODELS[model], "cache_dir": cache / "hub"}
    return score_text(TextModelSettings.model_validate(spec), rows)


def score_text(model: TextModelSettings, rows: list[Row]) -> list[float | None]:
    """Score every row through Modera's real serving path, in process."""
    category = next(iter(model.label_map.values()))
    with tempfile.TemporaryDirectory() as directory:
        policy = Path(directory) / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "policies": [
                        {
                            "id": "default",
                            "version": "scoring-only",
                            "text": [{"category": category, "review_threshold": 0.5}],
                        }
                    ]
                }
            )
        )
        key = secrets.token_urlsafe(32)
        settings = Settings(
            api_key=SecretStr(key), policy_file=policy, text_model=model
        )
        scores: list[float | None] = []
        with TestClient(
            create_app(settings), headers={"Authorization": f"Bearer {key}"}
        ) as client:
            for row in rows:
                response = client.post(
                    "/v1/moderations",
                    json={"input": {"type": "text", "text": row.text}},
                )
                if response.status_code != 200:
                    scores.append(None)
                    continue
                findings = {
                    f["category"]: f["score"] for f in response.json()["findings"]
                }
                scores.append(findings[category])
    return scores


def wilson(successes: int, total: int) -> tuple[float, float] | None:
    if total == 0:
        return None
    z, p = 1.96, successes / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    scale = 1 + z * z / total
    return round((centre - margin) / scale, 4), round((centre + margin) / scale, 4)


def rate(successes: int, total: int) -> dict[str, Any]:
    value = successes / total if total else None
    return {
        "value": None if value is None else round(value, 4),
        "count": successes,
        "of": total,
        "ci95": wilson(successes, total),
    }


def auc(positives: list[float], negatives: list[float]) -> float:
    ranked = sorted([(s, 1) for s in positives] + [(s, 0) for s in negatives])
    rank_sum, index = 0.0, 0
    while index < len(ranked):
        end = index
        while end < len(ranked) and ranked[end][0] == ranked[index][0]:
            end += 1
        average = (index + 1 + end) / 2
        rank_sum += average * sum(label for _, label in ranked[index:end])
        index = end
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def bootstrap_auc(
    positives: list[float], negatives: list[float]
) -> tuple[float, float]:
    generator = random.Random(20260511)
    samples = sorted(
        auc(
            generator.choices(positives, k=len(positives)),
            generator.choices(negatives, k=len(negatives)),
        )
        for _ in range(BOOTSTRAPS)
    )
    return round(samples[int(0.025 * BOOTSTRAPS)], 4), round(
        samples[int(0.975 * BOOTSTRAPS)], 4
    )


def cutoff_for(positives: list[float], negatives: list[float], max_fpr: float) -> float:
    """Lowest cutoff whose false-positive rate on these examples is within budget."""
    best = max(positives + negatives) + 1e-9
    for cutoff in sorted(set(positives + negatives)):
        if sum(s >= cutoff for s in negatives) / len(negatives) <= max_fpr:
            return cutoff
    return best


def at_cutoff(
    positives: list[float], negatives: list[float], cutoff: float
) -> dict[str, Any]:
    tp = sum(s >= cutoff for s in positives)
    fp = sum(s >= cutoff for s in negatives)
    return {
        "cutoff": round(cutoff, 4),
        "recall": rate(tp, len(positives)),
        "false_positive_rate": rate(fp, len(negatives)),
        "precision": rate(tp, tp + fp),
    }


def bands(
    positives: list[float], negatives: list[float], review: float, block: float
) -> dict[str, Any]:
    def split(scores: list[float]) -> dict[str, int]:
        return {
            "allow": sum(s < review for s in scores),
            "review": sum(review <= s < block for s in scores),
            "block": sum(s >= block for s in scores),
        }

    return {
        "review_cutoff": round(review, 4),
        "block_cutoff": round(block, 4),
        "toxic": split(positives),
        "clean": split(negatives),
    }


def brier(scores: list[float], labels: list[int]) -> float:
    return sum((s - y) ** 2 for s, y in zip(scores, labels, strict=True)) / len(scores)


def reliability(scores: list[float], labels: list[int]) -> list[dict[str, Any]]:
    """Equal-width score bins: how often the examples in each bin are really toxic."""
    table = []
    for index in range(BINS):
        low, high = index / BINS, (index + 1) / BINS
        members = [
            (s, y)
            for s, y in zip(scores, labels, strict=True)
            if low <= s < high or (index == BINS - 1 and s == 1.0)
        ]
        if members:
            table.append(
                {
                    "range": [low, high],
                    "n": len(members),
                    "mean_score": round(sum(s for s, _ in members) / len(members), 4),
                    "observed": round(sum(y for _, y in members) / len(members), 4),
                }
            )
    return table


def ece(scores: list[float], labels: list[int]) -> float:
    """Expected calibration error: the average gap between score and observed rate."""
    total: float = 0.0
    for item in reliability(scores, labels):
        total += item["n"] / len(scores) * abs(item["observed"] - item["mean_score"])
    return total


def isotonic(scores: list[float], labels: list[int]) -> Callable[[float], float]:
    """Monotone calibration map fitted by pooling adjacent violators."""
    blocks: list[list[float]] = []  # [sum of labels, count, highest score]
    for score, label in sorted(zip(scores, labels, strict=True)):
        if blocks and blocks[-1][2] == score:
            blocks[-1][0] += label
            blocks[-1][1] += 1
        else:
            blocks.append([float(label), 1.0, score])
        while len(blocks) > 1 and (
            blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]
        ):
            last = blocks.pop()
            blocks[-1][0] += last[0]
            blocks[-1][1] += last[1]
            blocks[-1][2] = last[2]
    uppers = [block[2] for block in blocks]
    values = [block[0] / block[1] for block in blocks]
    return lambda x: values[min(bisect_left(uppers, x), len(values) - 1)]


def prevalence_table(recall: float, false_positive_rate: float) -> list[dict[str, Any]]:
    """What a fixed recall and false-flag rate mean when toxic text is rare."""
    table = []
    for prevalence in PREVALENCES:
        caught = prevalence * recall
        wrong = (1 - prevalence) * false_positive_rate
        table.append(
            {
                "prevalence": prevalence,
                "flagged_per_1000": round(1000 * (caught + wrong), 1),
                "toxic_among_flagged": (
                    round(caught / (caught + wrong), 4) if caught + wrong else None
                ),
                "toxic_missed_per_1000": round(1000 * prevalence * (1 - recall), 1),
            }
        )
    return table


def calibration(
    validation: tuple[list[float], list[int]], test: tuple[list[float], list[int]]
) -> dict[str, Any]:
    fitted = isotonic(*validation)
    scores, labels = test
    mapped = [fitted(score) for score in scores]
    base_rate = sum(labels) / len(labels)
    return {
        "base_rate": round(base_rate, 4),
        "brier_constant_prediction": round(base_rate * (1 - base_rate), 4),
        "raw": {
            "brier": round(brier(scores, labels), 4),
            "ece": round(ece(scores, labels), 4),
        },
        "isotonic_fitted_on_validation": {
            "brier": round(brier(mapped, labels), 4),
            "ece": round(ece(mapped, labels), 4),
        },
        "reliability_raw": reliability(scores, labels),
    }


def evaluate(rows: list[Row], scores: list[float | None]) -> dict[str, Any]:
    failed = sum(score is None for score in scores)
    groups: dict[str, tuple[list[float], list[float]]] = {
        "validation": ([], []),
        "test": ([], []),
    }
    for row, score in zip(rows, scores, strict=True):
        if score is not None:
            groups[row.split][0 if row.positive else 1].append(score)
    (val_pos, val_neg), (test_pos, test_neg) = groups["validation"], groups["test"]
    review = cutoff_for(val_pos, val_neg, MAX_FPR)
    block = cutoff_for(val_pos, val_neg, BLOCK_FPR)
    chosen = at_cutoff(val_pos, val_neg, review)
    gate = (
        chosen["recall"]["value"] is not None
        and chosen["recall"]["value"] >= MIN_RECALL
    )
    held_out = at_cutoff(test_pos, test_neg, review)

    def labelled(pos: list[float], neg: list[float]) -> tuple[list[float], list[int]]:
        return pos + neg, [1] * len(pos) + [0] * len(neg)

    return {
        "examples": {
            s: {"toxic": len(p), "clean": len(n)} for s, (p, n) in groups.items()
        },
        "failed_requests": failed,
        "auc_test": {
            "value": round(auc(test_pos, test_neg), 4),
            "ci95": bootstrap_auc(test_pos, test_neg),
        },
        "auc_validation": round(auc(val_pos, val_neg), 4),
        "gate": {
            "max_false_positive_rate": MAX_FPR,
            "min_recall": MIN_RECALL,
            "validation_at_chosen_cutoff": chosen,
            "met_on_validation": gate,
        },
        "test_at_reference_cutoff": at_cutoff(test_pos, test_neg, REFERENCE_CUTOFF),
        "test_at_chosen_cutoff": held_out,
        "test_bands": bands(test_pos, test_neg, review, block),
        "calibration": calibration(
            labelled(val_pos, val_neg), labelled(test_pos, test_neg)
        ),
        "prevalence": prevalence_table(
            held_out["recall"]["value"], held_out["false_positive_rate"]["value"]
        ),
    }


def any_of(
    rows: list[Row], scores: dict[str, list[float | None]], results: dict[str, Any]
) -> dict[str, Any]:
    """Flag when either of the two best models (by validation AUC) crosses its cutoff.

    Each model gets half of the false-positive budget so the union stays near the
    same budget as a single model. This is an experiment about what a multi-model
    text policy could give; the service serves one text model today.
    """
    best = sorted(results, key=lambda m: results[m]["auc_validation"], reverse=True)[:2]
    cutoffs = {}
    for model in best:
        pos = [
            s
            for r, s in zip(rows, scores[model], strict=True)
            if s is not None and r.split == "validation" and r.positive
        ]
        neg = [
            s
            for r, s in zip(rows, scores[model], strict=True)
            if s is not None and r.split == "validation" and not r.positive
        ]
        cutoffs[model] = cutoff_for(pos, neg, MAX_FPR / 2)
    outcome: dict[str, dict[str, int]] = {
        split: {"tp": 0, "fp": 0, "toxic": 0, "clean": 0}
        for split in ("validation", "test")
    }
    for index, row in enumerate(rows):
        values = [scores[model][index] for model in best]
        if any(value is None for value in values):
            continue
        flagged = any(
            value is not None and value >= cutoffs[model]
            for model, value in zip(best, values, strict=True)
        )
        counts = outcome[row.split]
        counts["toxic" if row.positive else "clean"] += 1
        if flagged:
            counts["tp" if row.positive else "fp"] += 1
    summary = {
        split: {
            "recall": rate(c["tp"], c["toxic"]),
            "false_positive_rate": rate(c["fp"], c["clean"]),
        }
        for split, c in outcome.items()
    }
    return {
        "models": best,
        "each_model_false_positive_budget": MAX_FPR / 2,
        "cutoffs": {m: round(c, 4) for m, c in cutoffs.items()},
        "validation": summary["validation"],
        "test": summary["test"],
        "best_single_on_test": {m: results[m]["test_at_chosen_cutoff"] for m in best},
    }


def pct(item: dict[str, Any]) -> str:
    if item["value"] is None:
        return "n/a"
    low, high = item["ci95"]
    return f"{item['value']:.1%} ({low:.1%}-{high:.1%})"


def render(report: dict[str, Any]) -> str:
    out = [
        "# Text accuracy",
        "",
        "Measured with `python -m evaluation.text`. Thresholds are chosen on a "
        "validation half and every figure below comes from the held-out half. "
        "Ranges are 95% confidence intervals. The two datasets are never pooled.",
        "",
        f"**Target.** At most {MAX_FPR:.0%} of clean examples flagged and at least "
        f"{MIN_RECALL:.0%} of toxic ones caught, fixed before scoring. "
        "A model meets it only if the cutoff that holds false positives to "
        f"{MAX_FPR:.0%} on validation also reaches {MIN_RECALL:.0%} recall there.",
        "",
        "The main comparison and the bands were fixed before scoring. The calibration, "
        "prevalence and two-model sections were added after seeing the first results, "
        "so treat them as exploratory.",
        "",
    ]
    out += ["## Result", ""]
    for source, data in report["sources"].items():
        meeting = [
            model
            for model, result in data["models"].items()
            if result["gate"]["met_on_validation"]
        ]
        verdict = ", ".join(meeting) if meeting else "none of the models"
        out.append(f"- **{source}:** {verdict} met the target.")
    out += [
        "",
        "## Models",
        "",
        "| Model | Task | Licence | Revision |",
        "| --- | --- | --- | --- |",
    ]
    for model, spec in MODELS.items():
        note = MODEL_NOTES[model]
        out.append(
            f"| {model} (`{spec['id']}`) | {note['task']} | {note['licence']} | "
            f"`{spec['revision'][:12]}` |"
        )
    out.append("")
    for model_id, reason in EXCLUDED.items():
        out.append(f"Not evaluated: `{model_id}`, because {reason}.")
    out.append("")
    for source, data in report["sources"].items():
        spec = SOURCES[source]
        out += [
            f"## {source}",
            "",
            f"Dataset `{spec['repo']}` at `{spec['revision'][:12]}`; toxic means "
            f"{spec['positive']}. {data['cohort']['source_rows']} rows, "
            f"{data['cohort']['duplicates_collapsed']} duplicates collapsed, "
            f"{data['cohort']['conflicting_rows_dropped']} conflicting rows dropped, "
            f"{data['cohort']['too_long_excluded']} over the length limit excluded.",
            "",
            "| Model | Held-out toxic / clean | ROC AUC | Recall @0.5 | "
            "False flags @0.5 | Recall @ validation cutoff | "
            "False flags @ validation cutoff | Meets target |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for model, result in data["models"].items():
            t = result["examples"]["test"]
            ref, chosen = (
                result["test_at_reference_cutoff"],
                result["test_at_chosen_cutoff"],
            )
            low, high = result["auc_test"]["ci95"]
            out.append(
                f"| {model} | {t['toxic']} / {t['clean']} | "
                f"{result['auc_test']['value']:.3f} ({low:.3f}-{high:.3f}) | "
                f"{pct(ref['recall'])} | {pct(ref['false_positive_rate'])} | "
                f"{pct(chosen['recall'])} | {pct(chosen['false_positive_rate'])} | "
                f"{'yes' if result['gate']['met_on_validation'] else 'no'} |"
            )
        out += [
            "",
            "### Review and block bands",
            "",
            "A two-threshold policy: review at the cutoff that holds validation false "
            f"positives to {MAX_FPR:.0%}, block at the one that holds them to "
            f"{BLOCK_FPR:.0%}. Counts are held-out examples.",
            "",
            "| Model | Toxic: allow / review / block | Clean: allow / review / block |",
            "| --- | --- | --- |",
        ]
        for model, result in data["models"].items():
            b = result["test_bands"]
            t, c = b["toxic"], b["clean"]
            out.append(
                f"| {model} | {t['allow']} / {t['review']} / {t['block']} | "
                f"{c['allow']} / {c['review']} / {c['block']} |"
            )
        out += [
            "",
            "### Do the scores mean what they say?",
            "",
            "A score of 0.7 is only useful as a probability if about 70% of examples "
            "scored near 0.7 are toxic. Brier score is the mean squared error of the "
            "score as a probability (lower is better; a constant prediction of the "
            "base rate scores the first figure below). Expected calibration error "
            "(ECE) is the average gap between score and observed rate. The calibrated "
            "columns map scores with isotonic regression fitted on the validation "
            "half. A monotone map cannot change ranking, so AUC and the recall at a "
            "given false-flag rate stay the same.",
            "",
            f"Base rate on the held-out half and Brier of a constant prediction: "
            f"{next(iter(data['models'].values()))['calibration']['brier_constant_prediction']:.3f}.",
            "",
            "| Model | Brier raw | Brier calibrated | ECE raw | ECE calibrated |",
            "| --- | --- | --- | --- | --- |",
        ]
        for model, result in data["models"].items():
            c = result["calibration"]
            out.append(
                f"| {model} | {c['raw']['brier']:.3f} | "
                f"{c['isotonic_fitted_on_validation']['brier']:.3f} | "
                f"{c['raw']['ece']:.3f} | "
                f"{c['isotonic_fitted_on_validation']['ece']:.3f} |"
            )
        top = max(data["models"], key=lambda m: data["models"][m]["auc_test"]["value"])
        out += [
            "",
            f"Reliability of the highest-AUC model ({top}), raw scores, held-out half:",
            "",
            "| Score range | Examples | Mean score | Actually toxic |",
            "| --- | --- | --- | --- |",
        ]
        for item in data["models"][top]["calibration"]["reliability_raw"]:
            low, high = item["range"]
            out.append(
                f"| {low:.1f}-{high:.1f} | {item['n']} | "
                f"{item['mean_score']:.2f} | {item['observed']:.0%} |"
            )
        out += [
            "",
            "### What this means when toxic text is rare",
            "",
            "These test sets are about half toxic. Real traffic rarely is. Holding "
            "each "
            "model's held-out recall and false-flag rate fixed at the "
            f"{MAX_FPR:.0%}-budget cutoff, this is what a reviewer would see per "
            "1,000 items at different shares of toxic content. The last figure in each "
            "cell is toxic items that slipped through.",
            "",
            "| Model | " + " | ".join(f"{p:.0%} toxic" for p in PREVALENCES) + " |",
            "| --- | " + " | ".join("---" for _ in PREVALENCES) + " |",
        ]
        for model, result in data["models"].items():
            cells = []
            for item in result["prevalence"]:
                share = item["toxic_among_flagged"]
                cells.append(
                    f"{item['flagged_per_1000']:.0f} flagged, "
                    f"{'n/a' if share is None else f'{share:.0%}'} toxic, "
                    f"{item['toxic_missed_per_1000']:.0f} missed"
                )
            out.append(f"| {model} | " + " | ".join(cells) + " |")
        combo = data["combination"]
        a, b = combo["models"]
        out += [
            "",
            "### Does a second model help?",
            "",
            f"Flag an item if either {a} or {b} (the two best by validation AUC) "
            "crosses its own cutoff, each held to "
            f"{combo['each_model_false_positive_budget']:.1%} "
            "false flags on validation. This is an experiment about what a multi-model "
            "text policy could give. The service serves one text model, so this is "
            "not a shipped feature.",
            "",
            "| Rule | Recall | False flags |",
            "| --- | --- | --- |",
        ]
        for model, single in combo["best_single_on_test"].items():
            out.append(
                f"| {model} alone, 5% budget | {pct(single['recall'])} | "
                f"{pct(single['false_positive_rate'])} |"
            )
        out += [
            f"| Either of the two | {pct(combo['test']['recall'])} | "
            f"{pct(combo['test']['false_positive_rate'])} |",
            "",
        ]
    out += [
        "## Limits",
        "",
        "- External datasets, not your traffic. Rates here do not transfer to another "
        "audience, language mix or prevalence; these samples are balanced by "
        "construction, not representative.",
        "- Labels come from the datasets' own annotation, which has its own error. "
        "ToxiFrench uses a semi-automated pipeline with human checks, and ToxiGen "
        "uses model-generated text with human ratings.",
        "- Several models list the TextDetox multilingual toxicity data as training "
        "data, and the Horizon models list Civil Comments. No evaluated card names "
        "ToxiFrench or ToxiGen, but cards can be incomplete, so overlap with these "
        "test sets is not ruled out.",
        "- The split is a hash of the text, not time or author. Near-duplicates, "
        "shared "
        "authors and overlap with a model's training data are not ruled out.",
        "- Dataset and model terms are their own. Check them before reuse.",
        "",
    ]
    return "\n".join(out)


def prepare_models(cache: Path) -> None:
    hub = importlib.import_module("huggingface_hub")
    for model in MODELS.values():
        hub.snapshot_download(
            model["id"],
            revision=model["revision"],
            cache_dir=cache / "hub",
            allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"],
        )


def load_scores(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {"sources": {}}


def save_scores(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data) + "\n")
    temporary.replace(path)


def score_command(cache: Path, path: Path) -> None:
    prepare_models(cache)
    data = load_scores(path)
    for source in SOURCES:
        rows, cohort = build_cohort(source, cache)
        entry = data["sources"].setdefault(source, {"scores": {}})
        entry["cohort"] = cohort
        entry["rows"] = [{"split": r.split, "positive": r.positive} for r in rows]
        for model in MODELS:
            if len(entry["scores"].get(model, [])) == len(rows):
                continue
            print(f"scoring {source} with {model} ({len(rows)} examples)", flush=True)
            entry["scores"][model] = score_model(model, rows, cache)
            save_scores(path, data)


def report_command(path: Path, json_path: Path, markdown: Path) -> None:
    data = load_scores(path)
    report: dict[str, Any] = {"sources": {}}
    for source, entry in data["sources"].items():
        rows = [Row("", r["positive"], r["split"]) for r in entry["rows"]]
        results = {
            model: evaluate(rows, entry["scores"][model])
            for model in MODELS
            if model in entry["scores"]
        }
        report["sources"][source] = {
            "cohort": entry["cohort"],
            "models": results,
            "combination": any_of(rows, entry["scores"], results),
        }
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    markdown.write_text(render(report))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    score = commands.add_parser(
        "score", help="run the models and save raw scores (resumable)"
    )
    score.add_argument("--cache", type=Path, required=True)
    score.add_argument("--scores", type=Path, required=True)
    report = commands.add_parser("report", help="compute the report from saved scores")
    report.add_argument("--scores", type=Path, required=True)
    report.add_argument("--json", type=Path, required=True)
    report.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "score":
        score_command(args.cache, args.scores)
    else:
        report_command(args.scores, args.json, args.markdown)


if __name__ == "__main__":
    main()
