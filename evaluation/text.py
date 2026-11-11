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
    "Gravitee DistilBERT": {
        "id": "gravitee-io/distilbert-multilingual-toxicity-classifier",
        "revision": "865ad1b453a49e4a7f2471fc40b727ececd28314",
        "activation": "softmax",
        "label_map": {"toxic": "toxicity"},
    },
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
    """Score every row through Modera's real serving path, in process."""
    spec = {**MODELS[model], "cache_dir": cache / "hub"}
    with tempfile.TemporaryDirectory() as directory:
        policy = Path(directory) / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "policies": [
                        {
                            "id": "default",
                            "version": "accuracy-1",
                            "text": [
                                {
                                    "category": "toxicity",
                                    "review_threshold": 0.5,
                                    "block_threshold": 0.85,
                                }
                            ],
                        }
                    ]
                }
            )
        )
        key = secrets.token_urlsafe(32)
        settings = Settings(
            api_key=SecretStr(key),
            policy_file=policy,
            text_model=TextModelSettings.model_validate(spec),
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
                scores.append(findings["toxicity"])
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
    return {
        "examples": {
            s: {"toxic": len(p), "clean": len(n)} for s, (p, n) in groups.items()
        },
        "failed_requests": failed,
        "auc_test": {
            "value": round(auc(test_pos, test_neg), 4),
            "ci95": bootstrap_auc(test_pos, test_neg),
        },
        "gate": {
            "max_false_positive_rate": MAX_FPR,
            "min_recall": MIN_RECALL,
            "validation_at_chosen_cutoff": chosen,
            "met_on_validation": gate,
        },
        "test_at_reference_cutoff": at_cutoff(test_pos, test_neg, REFERENCE_CUTOFF),
        "test_at_chosen_cutoff": at_cutoff(test_pos, test_neg, review),
        "test_bands": bands(test_pos, test_neg, review, block),
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
        "Ranges are 95% "
        "confidence intervals. The two datasets are never pooled.",
        "",
        f"**Target.** At most {MAX_FPR:.0%} of clean examples flagged and at least "
        f"{MIN_RECALL:.0%} of toxic ones caught, fixed before scoring. "
        "A model meets it only if the cutoff that holds false positives to "
        f"{MAX_FPR:.0%} on validation also reaches {MIN_RECALL:.0%} recall there.",
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
        ]
        out += [
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
        out.append("")
    out += [
        "## Limits",
        "",
        "- External datasets, not your traffic. Rates here do not transfer to another "
        "audience, language mix or prevalence; these samples are balanced by "
        "construction, not representative.",
        "- Labels come from the datasets' own annotation, which has its own error. "
        "ToxiFrench uses a semi-automated pipeline with human checks, and ToxiGen "
        "uses model-generated text with human ratings.",
        "- Deduplication is on normalised exact text only. Near-duplicates, shared "
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, help="model and dataset cache directory")
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument(
        "--from-json",
        action="store_true",
        help="render --markdown from an existing --json without scoring anything",
    )
    args = parser.parse_args()
    if args.from_json:
        args.markdown.write_text(render(json.loads(args.json.read_text())))
        return
    if args.cache is None:
        parser.error("--cache is required unless --from-json is given")
    prepare_models(args.cache)
    report: dict[str, Any] = {"sources": {}}
    for source in SOURCES:
        rows, cohort = build_cohort(source, args.cache)
        report["sources"][source] = {"cohort": cohort, "models": {}}
        for model in MODELS:
            print(f"scoring {source} with {model} ({len(rows)} examples)", flush=True)
            report["sources"][source]["models"][model] = evaluate(
                rows, score_model(model, rows, args.cache)
            )
    args.json.write_text(json.dumps(report, indent=2) + "\n")
    args.markdown.write_text(render(report))


if __name__ == "__main__":
    main()
