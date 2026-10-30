import argparse
import hashlib
import importlib
import json
import sys
import unicodedata
from collections import Counter, defaultdict
from importlib.metadata import version
from pathlib import Path
from typing import Any

from evaluation.report import Action, Case, summarize
from modera.errors import APIError
from modera.inference import Classifier
from modera.model_config import ModelConfig, TextModelSettings
from modera.policies import Policy, PolicyRule, evaluate_policy
from modera.prepare_models import prepare_models
from modera.results import Coverage

DATASET = "gravitee-io/textdetox-multilingual-toxicity-dataset"
REVISION = "bc570f7558ab8cbd8d0d9a550c7406e8343fad90"


def fingerprint(text: str) -> str:
    text = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return hashlib.sha256(text.encode()).hexdigest()


def select_test(
    train: list[dict[str, Any]], test: list[dict[str, Any]], per_label: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if per_label < 1:
        raise ValueError("per_label must be positive")
    if not test:
        raise ValueError("test dataset must not be empty")
    training = {fingerprint(row["text"]) for row in train}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in test:
        if (
            not isinstance(row["text"], str)
            or not row["text"].strip()
            or type(row["toxic"]) is not int
            or row["toxic"] not in (0, 1)
            or not isinstance(row["language"], str)
            or not row["language"]
        ):
            raise ValueError("invalid source record")
        grouped[fingerprint(row["text"])].append(row)
    counts: Counter[str] = Counter()
    eligible: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    languages = sorted({row["language"] for row in test})
    for digest, rows in grouped.items():
        if digest in training:
            counts["training_overlap_groups_excluded"] += 1
            continue
        if len({row["toxic"] for row in rows}) > 1:
            counts["conflicting_label_groups_excluded"] += 1
            continue
        counts["duplicate_test_rows_collapsed"] += len(rows) - 1
        row = min(rows, key=lambda row: (row["language"], row["text"]))
        eligible[(row["language"], row["toxic"])].append(row)
    selected = []
    for language in languages:
        for label in (0, 1):
            candidates = sorted(
                eligible[(language, label)], key=lambda r: fingerprint(r["text"])
            )
            if len(candidates) < per_label:
                raise ValueError(
                    f"not enough decontaminated cases for {language}/{label}"
                )
            for row in candidates[:per_label]:
                digest = fingerprint(row["text"])
                selected.append(
                    {
                        "case": Case(
                            id=f"{language}-{digest}",
                            group=digest,
                            split="test",
                            input_type="text",
                            slice=language,
                            expected="block" if label else "allow",
                        ).model_dump(),
                        "text": row["text"],
                    }
                )
    return selected, dict(counts)


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(directory: Path, cache: Path, per_label: int) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    hub = importlib.import_module("huggingface_hub")
    arrow = importlib.import_module("pyarrow")
    ipc = importlib.import_module("pyarrow.ipc")
    datasets = {}
    hashes = {}
    for split in ("train", "test"):
        name = f"{split}/data-00000-of-00001.arrow"
        path = Path(
            hub.hf_hub_download(
                DATASET, name, repo_type="dataset", revision=REVISION, cache_dir=cache
            )
        )
        with arrow.memory_map(str(path), "r") as source:
            datasets[split] = ipc.open_stream(source).read_all().to_pylist()
        hashes[name] = digest_file(path)
    selected, exclusions = select_test(datasets["train"], datasets["test"], per_label)
    write_json(directory / "cases.json", selected)
    write_json(
        directory / "protocol.json",
        {
            "dataset": DATASET,
            "revision": REVISION,
            "license_declared": "openrail++",
            "source_sha256": hashes,
            "per_language_per_label": per_label,
            "selection": (
                "Lowest normalized-text SHA-256 per language/label after "
                "removing global train matches, conflicts and duplicates."
            ),
            "exclusions": exclusions,
            "cases_sha256": digest_file(directory / "cases.json"),
            "threshold": 0.5,
            "limitations": (
                "Publisher held-out test, not a new independent benchmark. "
                "Exact-match filtering does not audit paraphrases or either "
                "model pretraining. No threshold tuning; balanced data does "
                "not represent traffic prevalence."
            ),
        },
    )


def run(directory: Path, candidate: Path, cache: Path, output: Path) -> None:
    protocol = json.loads((directory / "protocol.json").read_bytes())
    if digest_file(directory / "cases.json") != protocol["cases_sha256"]:
        raise ValueError("frozen sample changed")
    if protocol["threshold"] != 0.5:
        raise ValueError("comparison threshold must remain fixed at 0.5")
    selected = json.loads((directory / "cases.json").read_bytes())
    cases = [Case.model_validate(row["case"]) for row in selected]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("frozen sample must be nonempty with unique IDs")
    if any(case.input_type != "text" for case in cases):
        raise ValueError("comparison requires text cases")
    settings = TextModelSettings.model_validate_json(candidate.read_bytes())
    if set(settings.label_map.values()) != {"toxicity"}:
        raise ValueError("candidate must map exactly one toxicity output")
    selection = settings.model_copy(update={"cache_dir": cache.resolve() / "hub"})
    adapter = Classifier(selection, threads=2)
    policy = Policy(
        id="binary",
        version="comparison-1",
        text=(PolicyRule(category="toxicity", block_threshold=0.5),),
    )
    output.mkdir(parents=True, exist_ok=False)
    rows: list[tuple[Case, Action | None]] = []
    slices: dict[str, list[tuple[Case, Action | None]]] = defaultdict(list)
    errors: Counter[str] = Counter()
    with (output / "predictions.jsonl").open("x") as raw:
        for index, row in enumerate(selected, 1):
            case = Case.model_validate(row["case"])
            predicted: Action | None = None
            scores = None
            error = None
            try:
                findings = adapter.predict(row["text"])
                scores = {finding.category: finding.score for finding in findings}
                predicted = evaluate_policy(
                    policy,
                    "text",
                    findings,
                    Coverage(evaluated=list(scores), unsupported=[], not_applicable=[]),
                ).action
            except APIError as exc:
                error = exc.code
            except Exception as exc:
                error = type(exc).__name__
            if error:
                errors[error] += 1
            raw.write(
                json.dumps(
                    {
                        "id": case.id,
                        "scores": scores,
                        "action": predicted,
                        "error": error,
                    },
                    allow_nan=False,
                )
                + "\n"
            )
            raw.flush()
            rows.append((case, predicted))
            slices[case.slice].append((case, predicted))
            if index % 100 == 0:
                print(f"Evaluated {index}/{len(selected)}", file=sys.stderr, flush=True)
    write_json(
        output / "report.json",
        {
            "status": "completed",
            "scope": (
                "Sequential local classifier adapter and policy; "
                "not HTTP latency/capacity."
            ),
            "protocol": protocol,
            "candidate": settings.model_dump(mode="json"),
            "overall": summarize(rows),
            "languages": {
                key: summarize(value) for key, value in sorted(slices.items())
            },
            "errors": dict(errors),
            "predictions_sha256": digest_file(output / "predictions.jsonl"),
            "packages": {
                name: version(name)
                for name in ("torch", "transformers", "huggingface-hub")
            },
            "adapter_sha256": digest_file(
                Path(__file__).parents[1] / "src/modera/inference.py"
            ),
            "collector_sha256": digest_file(Path(__file__)),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare pinned classifiers on decontaminated publisher test data"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--directory", type=Path, required=True)
    prep.add_argument("--cache", type=Path, required=True)
    prep.add_argument("--per-label", type=int, default=50)
    models = commands.add_parser("models")
    models.add_argument("--cache", type=Path, required=True)
    run_parser = commands.add_parser("run")
    for name in ("directory", "candidate", "cache", "output"):
        run_parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.directory, args.cache, args.per_label)
    elif args.command == "models":
        # Freeze this study's candidates; other modalities share the directory.
        for filename in ("gravitee-distilbert.json", "horizon-small.json"):
            path = Path(__file__).parent / "candidates" / filename
            selection = TextModelSettings.model_validate_json(path.read_bytes())
            selection = selection.model_copy(
                update={"cache_dir": args.cache.resolve() / "hub"}
            )
            prepare_models(ModelConfig(text=selection), threads=2, offline=False)
    else:
        run(args.directory, args.candidate, args.cache, args.output)


if __name__ == "__main__":
    main()
