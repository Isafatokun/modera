import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evaluation.calibrate import Budget, CalibrationCase, Observation, calibrate
from evaluation.compare_text import digest_file
from evaluation.review import compare, export, finalize, prepare, unseal

TAXONOMY = Path(__file__).parents[2] / "evaluation/taxonomy.json"


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value))


def read(path: Path) -> Any:
    return json.loads(path.read_bytes())


@pytest.fixture
def workflow(tmp_path: Path) -> tuple[Path, Path, Path]:
    groups: dict[str, list[str]] = {"validation": [], "test": []}
    for n in range(100):
        group = f"fixture-{n}"
        split = (
            "validation"
            if hashlib.sha256(("review-v1:" + group).encode()).digest()[0] < 128
            else "test"
        )
        if len(groups[split]) < 2:
            groups[split].append(group)
    items = []
    for values in groups.values():
        for group in values:
            for label in ("positive", "negative"):
                items.append(
                    {
                        "id": f"{group}-{label}",
                        "group": group,
                        "input_type": "text",
                        "slice": "synthetic-fixture",
                        "text": f"{label} software fixture {group}",
                    }
                )
    source = tmp_path / "source.json"
    save(
        source,
        {
            "origin": "synthetic",
            "prior_use": "diagnostic",
            "source_note": "Software fixtures, not real human evaluation.",
            "items": items,
        },
    )
    packet = tmp_path / "packet"
    prepare(source, TAXONOMY, packet)
    identifiers = {
        row["id"]: row["source_id"] for row in read(packet / "private-cases.json")
    }
    for name in ("reviewer-a", "reviewer-b"):
        path = packet / (name + ".json")
        r = read(path)
        r["origin"] = "human"
        for annotation in r["annotations"]:
            annotation["labels"] = {
                category: "absent" for category in annotation["labels"]
            }
            annotation["labels"]["targeted_abuse"] = (
                "present"
                if identifiers[annotation["id"]].endswith("positive")
                else "absent"
            )
        save(path, r)
    return packet, packet / "reviewer-a.json", packet / "reviewer-b.json"


def test_end_to_end_exports_validation_and_requires_frozen_score_mapping(
    workflow: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    packet, a, b = workflow
    visible = read(packet / "review-inputs.json")
    assert all(set(row) <= {"id", "input_type", "text", "context"} for row in visible)
    assert all(
        row["id"].startswith("case_") and "positive" not in row["id"] for row in visible
    )
    comp = tmp_path / "comparison"
    compare(packet, a, b, comp)
    reviewed = tmp_path / "reviewed"
    finalize(packet, a, b, comp / "comparison.json", None, reviewed)
    out = tmp_path / "export"
    export(
        packet,
        reviewed,
        "targeted_abuse",
        "toxicity",
        "Synthetic mapping test only",
        out,
    )
    cases = [CalibrationCase.model_validate(row) for row in read(out / "cases.json")]
    assert all(row.expected is None for row in cases if row.split == "test")
    results = []
    for n, row in enumerate([row for row in cases if row.split == "validation"]):
        results.append(
            {
                "id": row.id,
                "result": {
                    "request_id": f"req_{n:032x}",
                    "input_type": "text",
                    "policy": {"id": "test", "version": "1"},
                    "decision": {"action": "allow", "reasons": []},
                    "findings": [
                        {
                            "category": "toxicity",
                            "score": 0.9 if row.expected == "block" else 0.1,
                        }
                    ],
                    "coverage": {
                        "evaluated": ["toxicity"],
                        "unsupported": [],
                        "not_applicable": [],
                    },
                    "models": [{"id": "fixture/test", "revision": "a" * 40}],
                    "preprocessing_version": "fixture-1",
                },
            }
        )
    observations = [Observation.model_validate(r) for r in results]
    val = tmp_path / "validation.jsonl"
    val.write_text("".join(json.dumps(r) + "\n" for r in results))
    proposal = calibrate(
        cases,
        observations,
        "toxicity",
        Budget(max_false_positive_rate=0.05, min_recall=0.9),
    )
    proposal["inputs"] = {
        "cases_sha256": digest_file(out / "cases.json"),
        "validation_results_sha256": digest_file(val),
    }
    proposal["calibrator_sha256"] = "a" * 64
    prop = tmp_path / "proposal.json"
    save(prop, proposal)
    labels = tmp_path / "test-labels.json"
    unseal(reviewed, out, prop, val, labels)
    assert {row["expected"] for row in read(labels)} == {"allow", "block"}
    with pytest.raises(FileExistsError):
        unseal(reviewed, out, prop, val, labels)
    proposal["category"] = "insult"
    proposal["policy_rule"]["category"] = "insult"
    save(prop, proposal)
    with pytest.raises(ValueError, match="fingerprints"):
        unseal(reviewed, out, prop, val, tmp_path / "bad.json")
    assert read(reviewed / "provenance.json")["origin"] == "synthetic"


@pytest.mark.parametrize(
    "change",
    [
        "same_reviewer",
        "model_origin",
        "assisted_origin",
        "incomplete",
        "missing_category",
        "wrong_packet",
    ],
)
def test_invalid_reviews_never_finalize(
    workflow: tuple[Path, Path, Path], tmp_path: Path, change: str
) -> None:
    packet, a, b = workflow
    r = read(b)
    if change == "same_reviewer":
        r["reviewer_id"] = "reviewer-a"
    elif change == "model_origin":
        r["origin"] = "model"
    elif change == "assisted_origin":
        r["origin"] = "assisted_human"
    elif change == "incomplete":
        r["annotations"].pop()
    elif change == "missing_category":
        r["annotations"][0]["labels"].pop("threat")
    else:
        r["inputs_sha256"] = "0" * 64
    save(b, r)
    with pytest.raises(ValueError):
        compare(packet, a, b, tmp_path / "comparison")
    assert not (tmp_path / "comparison").exists()


def test_disagreement_requires_separate_human_and_uncertainty_is_excluded(
    workflow: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    packet, a, b = workflow
    r = read(b)
    r["annotations"][0]["labels"]["targeted_abuse"] = "uncertain"
    save(b, r)
    comp = tmp_path / "comparison"
    compare(packet, a, b, comp)
    with pytest.raises(ValueError, match="third human"):
        finalize(packet, a, b, comp / "comparison.json", None, tmp_path / "bad")
    adjud = read(comp / "adjudication-template.json")
    adjud["origin"] = "human"
    for row in adjud["resolutions"]:
        row.update(label="uncertain", rationale="Synthetic test; unresolved context.")
    path = tmp_path / "adjudication.json"
    save(path, adjud)
    reviewed = tmp_path / "reviewed"
    finalize(packet, a, b, comp / "comparison.json", path, reviewed)
    out = tmp_path / "export"
    export(packet, reviewed, "targeted_abuse", "toxicity", "Fixture mapping", out)
    assert read(out / "export.json")["uncertain_excluded"] == 1
    adjud["reviewer_id"] = "reviewer-a"
    save(path, adjud)
    with pytest.raises(ValueError, match="independent adjudicator"):
        finalize(packet, a, b, comp / "comparison.json", path, tmp_path / "invalid")


def test_changed_packet_is_rejected(
    workflow: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    packet, a, b = workflow
    save(packet / "review-inputs.json", [])
    with pytest.raises(ValueError, match="changed"):
        compare(packet, a, b, tmp_path / "comparison")


def test_duplicate_content_cannot_escape_source_group(tmp_path: Path) -> None:
    path = tmp_path / "source.json"
    save(
        path,
        {
            "origin": "synthetic",
            "prior_use": "diagnostic",
            "source_note": "Fixture",
            "items": [
                {
                    "id": "a",
                    "group": "one",
                    "input_type": "text",
                    "slice": "en",
                    "text": "Same text",
                },
                {
                    "id": "b",
                    "group": "two",
                    "input_type": "text",
                    "slice": "en",
                    "text": "same   text",
                },
            ],
        },
    )
    with pytest.raises(ValueError, match="same source group"):
        prepare(path, TAXONOMY, tmp_path / "packet")


@pytest.mark.parametrize("path", ["../outside.png", "/tmp/outside.png"])
def test_images_cannot_escape_intake_directory(tmp_path: Path, path: str) -> None:
    source = tmp_path / "source.json"
    save(
        source,
        {
            "origin": "synthetic",
            "prior_use": "diagnostic",
            "source_note": "Fixture",
            "items": [
                {
                    "id": "a",
                    "group": "one",
                    "input_type": "image",
                    "slice": "photos",
                    "image": path,
                },
                {
                    "id": "b",
                    "group": "two",
                    "input_type": "image",
                    "slice": "photos",
                    "image": path,
                },
            ],
        },
    )
    with pytest.raises(ValueError, match="inside"):
        prepare(source, TAXONOMY, tmp_path / "packet")


def test_reviewer_bundle_cannot_leak_private_cases_or_filled_labels(
    workflow: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    from zipfile import ZipFile

    from evaluation.review import bundle

    packet, _, _ = workflow
    output = tmp_path / "reviewer.zip"
    bundle(packet, "reviewer-a", output)
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "taxonomy.json",
            "review-inputs.json",
            "review.json",
        }
        form = json.loads(archive.read("review.json"))
        assert form["origin"] is None
        assert all(
            label is None
            for row in form["annotations"]
            for label in row["labels"].values()
        )
        assert all(
            "source_id" not in row and "group" not in row and "split" not in row
            for row in json.loads(archive.read("review-inputs.json"))
        )
    with pytest.raises(FileExistsError):
        bundle(packet, "reviewer-a", output)
