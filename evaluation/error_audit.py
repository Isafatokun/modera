"""Replay a deterministic external-error sample and audit label sensitivity."""

import argparse
import csv
import hashlib
import importlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from evaluation.compare_text import digest_file, fingerprint
from evaluation.diagnose import roc_auc
from evaluation.external_text import SOURCES, write
from modera.model_config import TextModelSettings
from modera.results import ModerationResult


def select_errors(
    cases: list[dict[str, Any]], results: dict[str, ModerationResult], count: int
) -> dict[str, list[dict[str, Any]]]:
    if count < 1 or not cases or len({row["id"] for row in cases}) != len(cases):
        raise ValueError("nonempty unique cases and positive sample size required")
    if set(results) != {row["id"] for row in cases}:
        raise ValueError("results must match test IDs exactly")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in cases:
        result = results[row["id"]]
        if result.fallback or result.input_type != "text":
            raise ValueError("audit requires completed native text results")
        predicted = result.decision.action != "allow"
        if predicted != row["positive"]:
            error = "false_negative" if row["positive"] else "false_positive"
            groups[row["source"] + "/" + error].append(row)
    return {
        group: sorted(
            rows,
            key=lambda row: hashlib.sha256(
                ("audit-v1:" + row["id"]).encode()
            ).hexdigest(),
        )[:count]
        for group, rows in sorted(groups.items())
    }


def metrics(rows: list[tuple[bool, float]]) -> dict[str, Any]:
    positives = sum(label for label, _ in rows)
    negatives = len(rows) - positives
    tp = sum(label and score >= 0.5 for label, score in rows)
    fp = sum(not label and score >= 0.5 for label, score in rows)
    return {
        "cases": len(rows),
        "positive_cases": positives,
        "negative_cases": negatives,
        "true_positive": tp,
        "false_positive": fp,
        "recall": tp / positives if positives else None,
        "false_positive_rate": fp / negatives if negatives else None,
        "roc_auc": roc_auc(rows),
    }


def audit(directory: Path, runs: Path, cache: Path, output: Path) -> None:
    protocol = json.loads((directory / "protocol.json").read_bytes())
    for name, digest in protocol["hashes"].items():
        if digest_file(directory / name) != digest:
            raise ValueError("original frozen source changed")
    cases = json.loads((directory / "sealed-test.json").read_bytes())
    hub = importlib.import_module("huggingface_hub")
    english_spec = SOURCES["toxigen"]
    raw_path = Path(
        hub.hf_hub_download(
            english_spec["repo"],
            english_spec["file"],
            revision=english_spec["revision"],
            repo_type="dataset",
            cache_dir=cache,
            local_files_only=True,
        )
    )
    if digest_file(raw_path) != protocol["sources"]["toxigen"]["sha256"]:
        raise ValueError("English annotation source changed")
    with raw_path.open(newline="") as stream:
        ratings = {
            "toxigen-" + fingerprint(row["text"]): float(row["toxicity_human"])
            for row in csv.DictReader(stream)
        }
    output.mkdir(parents=True, exist_ok=False)
    torch = importlib.import_module("torch")
    transformers = importlib.import_module("transformers")
    torch.set_num_threads(2)
    report: dict[str, Any] = {
        "sample_rule": "6 per model/source/error stratum, audit-v1 salted ID SHA-256",
        "inference_tolerance": 1e-6,
        "scope": (
            "Post-hoc diagnostics, no relabelling, threshold tuning "
            "or new acceptance test"
        ),
        "protocol_sha256": digest_file(directory / "protocol.json"),
        "models": {},
    }
    for candidate in ("horizon-small", "gravitee-distilbert"):
        root = runs / candidate
        recorded = json.loads((root / "report.json").read_bytes())
        if recorded["protocol_sha256"] != report["protocol_sha256"]:
            raise ValueError("run belongs to a different cohort")
        settings = TextModelSettings.model_validate(recorded["candidate"])
        if settings.model_dump(mode="json") != protocol["candidates"][candidate]:
            raise ValueError("model selection changed")
        rows = [
            json.loads(line)
            for line in (root / "test-results.jsonl").read_bytes().splitlines()
        ]
        results = {
            row["id"]: ModerationResult.model_validate(row["result"]) for row in rows
        }
        if len(results) != len(rows):
            raise ValueError("duplicate response IDs")
        sample = select_errors(cases, results, 6)
        options = {
            "revision": settings.revision,
            "cache_dir": cache.resolve() / "hub",
            "local_files_only": True,
            "trust_remote_code": False,
        }
        tokenizer = transformers.AutoTokenizer.from_pretrained(settings.id, **options)
        model = transformers.AutoModelForSequenceClassification.from_pretrained(
            settings.id, **options, use_safetensors=True, dtype=torch.float32
        )
        model.eval()
        labels = model.config.id2label
        native = next(
            label
            for label, category in settings.label_map.items()
            if category == "toxicity"
        )
        index = next(index for index, label in labels.items() if label == native)
        multi_label = model.config.problem_type == "multi_label_classification"
        expected_activation = "sigmoid" if multi_label else "softmax"
        if settings.activation != expected_activation:
            raise ValueError("activation differs from checkpoint problem type")
        checks = []
        for group, selected in sample.items():
            for case in selected:
                inputs = tokenizer(case["text"], return_tensors="pt", truncation=False)
                with torch.inference_mode():
                    logits = model(**inputs).logits
                    score = (
                        torch.sigmoid(logits)
                        if multi_label
                        else torch.softmax(logits, dim=-1)
                    )[0, index].item()
                saved = next(
                    f.score
                    for f in results[case["id"]].findings
                    if f.category == "toxicity"
                )
                if inputs["input_ids"].shape[1] > settings.max_tokens:
                    raise ValueError("sample exceeds configured token limit")
                checks.append(
                    {
                        "id": case["id"],
                        "stratum": group,
                        "input_sha256": hashlib.sha256(
                            case["text"].encode()
                        ).hexdigest(),
                        "tokens": inputs["input_ids"].shape[1],
                        "recorded_api_score": saved,
                        "direct_score": score,
                        "absolute_difference": abs(score - saved),
                        "matches": abs(score - saved) <= 1e-6,
                    }
                )
        english = [row for row in cases if row["source"] == "toxigen"]

        def score(
            identifier: str, observed: dict[str, ModerationResult] = results
        ) -> float:
            return next(
                f.score
                for f in observed[identifier].findings
                if f.category == "toxicity"
            )

        source_metrics = {
            source: metrics(
                [
                    (row["positive"], score(row["id"]))
                    for row in cases
                    if row["source"] == source
                ]
            )
            for source in SOURCES
        }
        sensitivity = {
            "recorded_rating_gte_3": metrics(
                [(ratings[row["id"]] >= 3, score(row["id"])) for row in english]
            ),
            "alternative_rating_gt_2_5": metrics(
                [(ratings[row["id"]] > 2.5, score(row["id"])) for row in english]
            ),
            "extreme_mean_ratings_lte_2_or_gte_4": metrics(
                [
                    (ratings[row["id"]] >= 4, score(row["id"]))
                    for row in english
                    if ratings[row["id"]] <= 2 or ratings[row["id"]] >= 4
                ]
            ),
            "interpretation": (
                "Post-hoc label-mapping sensitivity; "
                "extreme means do not prove annotator agreement"
            ),
        }
        report["models"][candidate] = {
            "model": settings.model_dump(mode="json"),
            "native_labels": labels,
            "problem_type": model.config.problem_type,
            "activation_checked": expected_activation,
            "sample_replays": checks,
            "all_sample_scores_match": all(row["matches"] for row in checks),
            "ranking_by_source": source_metrics,
            "toxigen_label_sensitivity": sensitivity,
            "test_response_sha256": digest_file(root / "test-results.jsonl"),
        }
        print(
            candidate,
            "replayed",
            len(checks),
            "matches",
            all(row["matches"] for row in checks),
            flush=True,
        )
        del model
    write(output / "report.json", report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.directory, args.runs, args.cache, args.output)


if __name__ == "__main__":
    main()
