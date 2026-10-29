"""External-source text evaluation through Modera's in-process HTTP API."""

import argparse
import csv
import hashlib
import importlib
import json
import secrets
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from evaluation.assess import (
    AssessmentObservation,
    Fingerprints,
    FrozenProposal,
    HeldOutLabel,
    assess,
    assessment_metrics,
)
from evaluation.calibrate import Budget, CalibrationCase, Observation, calibrate
from evaluation.compare_text import digest_file, fingerprint
from modera.config import Settings
from modera.main import create_app
from modera.model_config import TextModelSettings
from modera.results import ModerationResult

SOURCES = {
    "toxifrench": {
        "repo": "AxelDlv00/ToxiFrench",
        "revision": "6280dba8a77ff27ca866a91b1098a6f4f24934e3",
        "file": "data/test_dataset.csv",
        "delimiter": ";",
        "text": "content",
        "label": "literal_conclusion_annotator",
    },
    "toxigen": {
        "repo": "toxigen/toxigen-data",
        "revision": "ea082a8973a287097b17973343576e0c8da8eb3b",
        "file": "annotated_test.csv",
        "delimiter": ",",
        "text": "text",
        "label": "toxicity_human",
    },
}
LIMITATIONS = (
    "External-source diagnostic, not a representative application sample or proof of "
    "training independence. Deduplication checks normalized exact text only, not "
    "authors, threads, prompts, translations or near duplicates. ToxiFrench labels "
    "use a semi-automated pipeline with human verification; ToxiGen text is generated "
    "and human-rated. Separate dataset rates must not be pooled "
    "into a production claim. "
    "The supported-length cohort is frozen using both pinned tokenizers "
    "before scoring; "
    "excluded cases remain counted separately. No serving defaults are changed."
)


def write(path: Path, value: Any) -> None:
    with path.open("x") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")


def labelled_rows(source: str, path: Path) -> list[dict[str, Any]]:
    spec = SOURCES[source]
    output = []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream, delimiter=spec["delimiter"]):
            text = row[spec["text"]]
            label = row[spec["label"]]
            if not text.strip():
                raise ValueError("empty source text")
            if source == "toxifrench":
                if label not in {"oui", "non"}:
                    raise ValueError("unknown French label")
                positive = label == "oui"
            else:
                rating = float(label)
                if not 1 <= rating <= 5:
                    raise ValueError("invalid human toxicity rating")
                positive = rating >= 3
            output.append({"text": text, "positive": positive})
    return output


def deduplicate(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[fingerprint(row["text"])].append(row)
    kept = []
    counts: Counter[str] = Counter()
    for group, members in sorted(groups.items()):
        if len({row["positive"] for row in members}) != 1:
            counts["conflicting_rows_excluded"] += len(members)
            continue
        counts["duplicate_rows_collapsed"] += len(members) - 1
        kept.append({**min(members, key=lambda row: row["text"]), "group": group})
    return kept, dict(counts)


def prepare(directory: Path, cache: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    hub = importlib.import_module("huggingface_hub")
    transformers = importlib.import_module("transformers")
    tokenizers = []
    candidate_root = Path(__file__).parent / "candidates"
    candidates = {}
    for path in sorted(candidate_root.glob("*.json")):
        model = TextModelSettings.model_validate_json(path.read_bytes())
        candidates[path.stem] = model.model_dump(mode="json")
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            model.id,
            revision=model.revision,
            cache_dir=cache.resolve() / "hub",
            local_files_only=True,
            trust_remote_code=False,
        )
        tokenizers.append(
            (tokenizer, min(model.max_tokens, tokenizer.model_max_length))
        )
    records, exclusions, sources = [], {}, {}
    for source, spec in SOURCES.items():
        path = Path(
            hub.hf_hub_download(
                spec["repo"],
                spec["file"],
                revision=spec["revision"],
                repo_type="dataset",
                cache_dir=cache,
            )
        )
        original = labelled_rows(source, path)
        rows, counts = deduplicate(original)
        eligible = []
        excluded = []
        for row in rows:
            valid = len(row["text"]) <= 4096 and all(
                len(tokenizer(row["text"], truncation=False)["input_ids"]) <= limit
                for tokenizer, limit in tokenizers
            )
            if not valid:
                excluded.append(
                    {"id": source + "-" + row["group"], "reason": "input_limits"}
                )
                continue
            digest = hashlib.sha256(("external-v1:" + row["group"]).encode()).digest()
            split = "validation" if digest[0] < 128 else "test"
            eligible.append(
                {
                    **row,
                    "id": source + "-" + row["group"],
                    "split": split,
                    "source": source,
                }
            )
        records.extend(eligible)
        exclusions[source] = {
            "source_rows": len(original),
            **counts,
            "input_limit_exclusions": len(excluded),
            "eligible": len(eligible),
        }
        sources[source] = {**spec, "sha256": digest_file(path)}
        write(directory / (source + "-exclusions.json"), excluded)
    manifest = [
        {
            "id": row["id"],
            "group": row["group"],
            "split": row["split"],
            "input_type": "text",
            "slice": row["source"],
            **(
                {"expected": "block" if row["positive"] else "allow"}
                if row["split"] == "validation"
                else {}
            ),
        }
        for row in records
    ]
    write(directory / "cases.json", manifest)
    write(
        directory / "validation-inputs.json",
        [
            {"id": row["id"], "text": row["text"]}
            for row in records
            if row["split"] == "validation"
        ],
    )
    write(
        directory / "sealed-test.json",
        [row for row in records if row["split"] == "test"],
    )
    write(
        directory / "protocol.json",
        {
            "version": "external-v1",
            "sources": sources,
            "candidates": candidates,
            "exclusions": exclusions,
            "english_positive_label": "toxicity_human >= 3 on scale 1..5",
            "budget": {"max_false_positive_rate": 0.05, "min_recall": 0.90},
            "budget_scope": (
                "Exploratory review gate, separately per dataset; not a production SLA"
            ),
            "split": (
                "normalized exact-text group SHA-256 salted external-v1; "
                "first byte <128 validation"
            ),
            "hashes": {
                name: digest_file(directory / name)
                for name in ("cases.json", "validation-inputs.json", "sealed-test.json")
            },
            "limitations": LIMITATIONS,
        },
    )


def evaluate(directory: Path, candidate: Path, cache: Path, output: Path) -> None:
    protocol = json.loads((directory / "protocol.json").read_bytes())
    for name, digest in protocol["hashes"].items():
        if digest_file(directory / name) != digest:
            raise ValueError("frozen inputs changed")
    selection = TextModelSettings.model_validate_json(candidate.read_bytes())
    if selection.model_dump(mode="json") != protocol["candidates"][candidate.stem]:
        raise ValueError("candidate changed after cohort selection")
    model = selection.model_copy(update={"cache_dir": cache.resolve() / "hub"})
    output.mkdir(parents=True, exist_ok=False)
    policy_path = output / "policy.json"
    write(
        policy_path,
        {
            "policies": [
                {
                    "id": "default",
                    "version": "external-fixed-1",
                    "text": [
                        {
                            "category": "toxicity",
                            "review_threshold": 0.5,
                            "block_threshold": 0.85,
                        }
                    ],
                }
            ]
        },
    )
    key = secrets.token_urlsafe(32)
    settings = Settings(
        api_key=SecretStr(key), text_model=model, policy_file=policy_path
    )
    manifest = [
        CalibrationCase.model_validate(row)
        for row in json.loads((directory / "cases.json").read_bytes())
    ]
    validations = json.loads((directory / "validation-inputs.json").read_bytes())
    frozen: dict[str, dict[str, Any]] = {}
    proposals: dict[str, dict[str, Any]] = {}
    results: dict[str, ModerationResult] = {}
    failures: dict[str, str] = {}
    with TestClient(
        create_app(settings), headers={"Authorization": "Bearer " + key}
    ) as client:

        def collect(rows: list[dict[str, Any]], name: str) -> None:
            with (output / name).open("x") as stream:
                for index, row in enumerate(rows, 1):
                    response = client.post(
                        "/v1/moderations",
                        json={"input": {"type": "text", "text": row["text"]}},
                    )
                    if response.status_code == 200:
                        result = ModerationResult.model_validate(response.json())
                        results[row["id"]] = result
                        record = {
                            "id": row["id"],
                            "result": result.model_dump(mode="json"),
                        }
                    else:
                        code = response.json()["error"]["code"]
                        failures[row["id"]] = code
                        record = {"id": row["id"], "error": code}
                    stream.write(json.dumps(record, allow_nan=False) + "\n")
                    stream.flush()
                    if index % 100 == 0:
                        print(
                            f"{candidate.stem} {name}: {index}/{len(rows)}", flush=True
                        )

        collect(validations, "validation-results.jsonl")
        proposal: dict[str, Any]
        for source in SOURCES:
            cases = [case for case in manifest if case.slice == source]
            validation_cases = [case for case in cases if case.split == "validation"]
            source_cases = output / (source + "-cases.json")
            source_validation = output / (source + "-validation-results.jsonl")
            write(
                source_cases,
                [case.model_dump(mode="json", exclude_none=True) for case in cases],
            )
            with source_validation.open("x") as stream:
                for case in validation_cases:
                    record = (
                        {
                            "id": case.id,
                            "result": results[case.id].model_dump(mode="json"),
                        }
                        if case.id in results
                        else {"id": case.id, "error": failures[case.id]}
                    )
                    stream.write(json.dumps(record) + "\n")
            if any(case.id in failures for case in validation_cases):
                proposal = {
                    "status": "incomplete",
                    "reason": "validation request failed",
                }
            else:
                observations = [
                    Observation(id=case.id, result=results[case.id])
                    for case in validation_cases
                ]
                proposal = calibrate(
                    cases,
                    observations,
                    "toxicity",
                    Budget.model_validate(protocol["budget"]),
                )
                if proposal["status"] == "proposal":
                    proposal["inputs"] = Fingerprints(
                        cases_sha256=digest_file(source_cases),
                        validation_results_sha256=digest_file(source_validation),
                    ).model_dump()
                    proposal["calibrator_sha256"] = digest_file(
                        Path(__file__).with_name("calibrate.py")
                    )
            write(output / (source + "-proposal.json"), proposal)
            frozen[source] = proposal
            proposals[source] = {
                "status": proposal["status"],
                "threshold": proposal.get("threshold"),
                "validation_metrics": proposal.get("validation_metrics"),
            }
        # Test labels and texts are read only after every proposal has been written.
        test = json.loads((directory / "sealed-test.json").read_bytes())
        collect(test, "test-results.jsonl")
    reports = {}
    for source in SOURCES:
        subset = [row for row in test if row["source"] == source]
        write(
            output / (source + "-test-labels.json"),
            [
                {"id": row["id"], "expected": "block" if row["positive"] else "allow"}
                for row in subset
            ],
        )
        with (output / (source + "-test-results.jsonl")).open("x") as stream:
            for row in subset:
                record = (
                    {
                        "id": row["id"],
                        "result": results[row["id"]].model_dump(mode="json"),
                    }
                    if row["id"] in results
                    else {"id": row["id"], "error": failures[row["id"]]}
                )
                stream.write(json.dumps(record) + "\n")
        fixed_rows = [
            (
                row["positive"],
                results[row["id"]].decision.action != "allow"
                if row["id"] in results
                else None,
            )
            for row in subset
        ]
        report: dict[str, Any] = {
            "reference_review_0_5": assessment_metrics(fixed_rows),
            "proposal": proposals[source],
            "test_errors": dict(
                Counter(failures[row["id"]] for row in subset if row["id"] in failures)
            ),
        }
        proposal = frozen[source]
        if proposal["status"] == "proposal":
            cases = [case for case in manifest if case.slice == source]
            assessment = assess(
                cases,
                [
                    HeldOutLabel(
                        id=row["id"], expected="block" if row["positive"] else "allow"
                    )
                    for row in subset
                ],
                [
                    AssessmentObservation(
                        id=row["id"],
                        result=results.get(row["id"]),
                        error=failures.get(row["id"]),
                    )
                    for row in subset
                ],
                FrozenProposal.model_validate(proposal),
                [
                    Observation(id=case.id, result=results[case.id])
                    for case in cases
                    if case.split == "validation"
                ],
            )
            write(output / (source + "-assessment.json"), assessment)
            report["held_out_assessment"] = assessment
        reports[source] = report
    write(
        output / "report.json",
        {
            "candidate": selection.model_dump(mode="json"),
            "protocol_sha256": digest_file(directory / "protocol.json"),
            "protocol": protocol,
            "collector_sha256": digest_file(Path(__file__)),
            "api_transport": "in-process ASGI HTTP, actual serving engine",
            "results": reports,
            "limitations": LIMITATIONS,
        },
    )
    print(json.dumps(reports, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.directory, args.cache)
    else:
        if args.candidate is None or args.output is None:
            parser.error("run requires --candidate and --output")
        evaluate(args.directory, args.candidate, args.cache, args.output)


if __name__ == "__main__":
    main()
