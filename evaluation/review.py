"""Blinded category annotation, disagreement tracking and sealed label export."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal
from zipfile import ZIP_DEFLATED, ZipFile

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evaluation.assess import FrozenProposal
from evaluation.calibrate import (
    CalibrationCase,
    Observation,
    calibrate,
    partition_cases,
)
from evaluation.compare_text import digest_file, fingerprint
from evaluation.external_text import write
from evaluation.report import Name
from modera.images import validate_image
from modera.policies import PolicyRule

Label = Literal["present", "absent", "uncertain"]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CategoryDefinition(Strict):
    input_type: Literal["text", "image"]
    definition: Annotated[str, Field(min_length=1)]


class Taxonomy(Strict):
    version: Name
    status: Name
    labels: list[str]
    categories: dict[str, CategoryDefinition]
    instructions: str

    @model_validator(mode="after")
    def validate_categories(self) -> "Taxonomy":
        if (
            set(self.labels) != {"present", "absent", "uncertain"}
            or not self.categories
        ):
            raise ValueError("taxonomy requires three labels and category definitions")
        return self


class Item(Strict):
    id: Identifier
    group: Name
    input_type: Literal["text", "image"]
    slice: Name
    text: str | None = None
    image: str | None = None
    context: str = ""

    @model_validator(mode="after")
    def validate_content(self) -> "Item":
        if self.input_type == "text":
            if not self.text or not self.text.strip() or self.image is not None:
                raise ValueError("text item requires only nonblank text")
        elif self.image is None or self.text is not None:
            raise ValueError("image item requires only a local image path")
        return self


class Intake(Strict):
    origin: Literal["public_corpus", "user_content", "synthetic"]
    prior_use: Literal["unseen_declared", "diagnostic", "training", "unknown"]
    source_note: Annotated[str, Field(min_length=1, max_length=2000)]
    items: list[Item] = Field(min_length=2, max_length=10000)


class Annotation(Strict):
    id: Identifier
    labels: dict[str, Label]
    notes: str


class Submission(Strict):
    reviewer_id: Identifier
    origin: Literal["human", "assisted_human", "model", "synthetic"]
    taxonomy_sha256: Digest
    inputs_sha256: Digest
    annotations: list[Annotation]


class Resolution(Strict):
    id: Identifier
    category: Name
    label: Label
    rationale: Annotated[str, Field(min_length=1)]

    @field_validator("rationale")
    @classmethod
    def nonblank_rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("adjudication rationale must not be blank")
        return value


class Adjudication(Strict):
    reviewer_id: Identifier
    origin: Literal["human", "assisted_human", "model", "synthetic"]
    comparison_sha256: Digest
    resolutions: list[Resolution]


def categories(taxonomy: Taxonomy, kind: str) -> set[str]:
    return {
        name
        for name, category in taxonomy.categories.items()
        if category.input_type == kind
    }


def prepare(source: Path, taxonomy_path: Path, output: Path) -> None:
    intake = Intake.model_validate_json(source.read_bytes())
    taxonomy = Taxonomy.model_validate_json(taxonomy_path.read_bytes())
    if len({item.id for item in intake.items}) != len(intake.items):
        raise ValueError("item IDs must be unique")
    inputs, private = [], []
    assets: dict[str, bytes] = {}
    content_groups: dict[str, str] = {}
    for item in intake.items:
        if not categories(taxonomy, item.input_type):
            raise ValueError("modality has no annotation definitions")
        blind_id = "case_" + hashlib.sha256(item.id.encode()).hexdigest()
        visible: dict[str, Any] = {
            "id": blind_id,
            "input_type": item.input_type,
            "context": item.context,
        }
        if item.input_type == "text":
            assert item.text is not None
            visible["text"] = item.text
            content_digest = fingerprint(item.text)
        else:
            assert item.image is not None
            relative = Path(item.image)
            root = source.resolve().parent
            path = (root / relative).resolve()
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not path.is_relative_to(root)
            ):
                raise ValueError("images must stay inside the intake directory")
            with path.open("rb") as source_image:
                data = source_image.read(8 * 1024 * 1024 + 1)
            validate_image(data)
            content_digest = hashlib.sha256(data).hexdigest()
            asset = "assets/" + content_digest
            assets[asset] = data
            visible["image"] = asset
        group_key = item.input_type + ":" + content_digest
        if group_key in content_groups and content_groups[group_key] != item.group:
            raise ValueError("duplicate content must remain in the same source group")
        content_groups[group_key] = item.group
        digest = hashlib.sha256(("review-v1:" + item.group).encode()).digest()
        split = "validation" if digest[0] < 128 else "test"
        inputs.append(visible)
        private.append(
            {
                "id": blind_id,
                "source_id": item.id,
                "group": item.group,
                "input_type": item.input_type,
                "slice": item.slice,
                "split": split,
            }
        )
    if {row["split"] for row in private} != {"validation", "test"}:
        raise ValueError("source groups must reserve both validation and test cases")
    output.mkdir(parents=True, exist_ok=False)
    (output / "assets").mkdir()
    for name, data in assets.items():
        (output / name).write_bytes(data)
    write(output / "taxonomy.json", taxonomy.model_dump(mode="json"))
    write(output / "review-inputs.json", inputs)
    write(output / "private-cases.json", private)
    protocol = {
        "taxonomy_sha256": digest_file(output / "taxonomy.json"),
        "inputs_sha256": digest_file(output / "review-inputs.json"),
        "cases_sha256": digest_file(output / "private-cases.json"),
        "assets": {
            name: hashlib.sha256(data).hexdigest() for name, data in assets.items()
        },
        "origin": intake.origin,
        "prior_use": intake.prior_use,
        "source_note": intake.source_note,
        "source_sha256": digest_file(source),
        "limitations": (
            "Reviewer origin and source independence are declarations, "
            "not identity or provenance verification. Exact duplicates are "
            "checked; hidden authors, threads, transformations and "
            "training overlap are not."
        ),
    }
    for reviewer in ("reviewer-a", "reviewer-b"):
        write(
            output / (reviewer + ".json"),
            {
                "reviewer_id": reviewer,
                "origin": None,
                "taxonomy_sha256": protocol["taxonomy_sha256"],
                "inputs_sha256": protocol["inputs_sha256"],
                "annotations": [
                    {
                        "id": row["id"],
                        "labels": {
                            name: None
                            for name in sorted(categories(taxonomy, row["input_type"]))
                        },
                        "notes": "",
                    }
                    for row in inputs
                ],
            },
        )
    # The completed protocol is the ready marker for a packet.
    write(output / "protocol.json", protocol)


def packet(directory: Path) -> tuple[dict[str, Any], Taxonomy, list[dict[str, Any]]]:
    protocol = json.loads((directory / "protocol.json").read_bytes())
    for name, key in (
        ("taxonomy.json", "taxonomy_sha256"),
        ("review-inputs.json", "inputs_sha256"),
        ("private-cases.json", "cases_sha256"),
    ):
        if digest_file(directory / name) != protocol[key]:
            raise ValueError("packet content changed after preparation")
    for name, digest in protocol["assets"].items():
        if digest_file(directory / name) != digest:
            raise ValueError("review image changed")
    return (
        protocol,
        Taxonomy.model_validate_json((directory / "taxonomy.json").read_bytes()),
        json.loads((directory / "private-cases.json").read_bytes()),
    )


def bundle(directory: Path, reviewer: str, output: Path) -> None:
    protocol, taxonomy, cases = packet(directory)
    if reviewer not in {"reviewer-a", "reviewer-b"}:
        raise ValueError("choose a blinded reviewer role")
    # Always build a blank form, even if an operator edited the original template.
    form = {
        "reviewer_id": reviewer,
        "origin": None,
        "taxonomy_sha256": protocol["taxonomy_sha256"],
        "inputs_sha256": protocol["inputs_sha256"],
        "annotations": [
            {
                "id": row["id"],
                "labels": {
                    category: None
                    for category in sorted(categories(taxonomy, row["input_type"]))
                },
                "notes": "",
            }
            for row in cases
        ],
    }
    with output.open("xb") as target, ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name in [
            "taxonomy.json",
            "review-inputs.json",
            *sorted(protocol["assets"]),
        ]:
            archive.write(directory / name, name)
        archive.writestr("review.json", json.dumps(form, indent=2) + "\n")


def submission(
    path: Path,
    protocol: dict[str, Any],
    taxonomy: Taxonomy,
    cases: list[dict[str, Any]],
) -> Submission:
    result = Submission.model_validate_json(path.read_bytes())
    if result.origin != "human":
        raise ValueError("independent review requires declared human submissions")
    if (
        result.inputs_sha256 != protocol["inputs_sha256"]
        or result.taxonomy_sha256 != protocol["taxonomy_sha256"]
    ):
        raise ValueError("submission belongs to a different packet")
    observed = {row.id: row for row in result.annotations}
    if len(observed) != len(result.annotations) or set(observed) != {
        row["id"] for row in cases
    }:
        raise ValueError("annotate every case exactly once")
    for row in cases:
        if set(observed[row["id"]].labels) != categories(taxonomy, row["input_type"]):
            raise ValueError("annotate every applicable category exactly once")
    return result


def compare(directory: Path, first: Path, second: Path, output: Path) -> None:
    protocol, taxonomy, cases = packet(directory)
    a, b = [submission(path, protocol, taxonomy, cases) for path in (first, second)]
    if a.reviewer_id == b.reviewer_id:
        raise ValueError("reviewers must have distinct IDs")
    left, right = ({row.id: row for row in review.annotations} for review in (a, b))
    disagreements = []
    for row in cases:
        for category in sorted(categories(taxonomy, row["input_type"])):
            av, bv = left[row["id"]].labels[category], right[row["id"]].labels[category]
            if av != bv:
                disagreements.append(
                    {
                        "id": row["id"],
                        "category": category,
                        "first": av,
                        "second": bv,
                        "first_notes": left[row["id"]].notes,
                        "second_notes": right[row["id"]].notes,
                    }
                )
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "packet_sha256": digest_file(directory / "protocol.json"),
        "first_sha256": digest_file(first),
        "second_sha256": digest_file(second),
        "reviewers": [a.reviewer_id, b.reviewer_id],
        "disagreements": disagreements,
    }
    write(output / "comparison.json", report)
    write(
        output / "adjudication-template.json",
        {
            "reviewer_id": "reviewer-c",
            "origin": None,
            "comparison_sha256": digest_file(output / "comparison.json"),
            "resolutions": [
                {
                    "id": row["id"],
                    "category": row["category"],
                    "label": None,
                    "rationale": "",
                }
                for row in disagreements
            ],
        },
    )


def finalize(
    directory: Path,
    first: Path,
    second: Path,
    comparison: Path,
    adjudication_path: Path | None,
    output: Path,
) -> None:
    protocol, taxonomy, cases = packet(directory)
    a, b = [submission(path, protocol, taxonomy, cases) for path in (first, second)]
    report = json.loads(comparison.read_bytes())
    if (
        a.reviewer_id == b.reviewer_id
        or report["packet_sha256"] != digest_file(directory / "protocol.json")
        or report["first_sha256"] != digest_file(first)
        or report["second_sha256"] != digest_file(second)
    ):
        raise ValueError("review inputs differ from the comparison")
    left, right = ({row.id: row for row in review.annotations} for review in (a, b))
    expected = {
        (row["id"], category)
        for row in cases
        for category in categories(taxonomy, row["input_type"])
        if left[row["id"]].labels[category] != right[row["id"]].labels[category]
    }
    if expected != {(row["id"], row["category"]) for row in report["disagreements"]}:
        raise ValueError("comparison disagreements changed")
    resolutions: dict[tuple[str, str], Label] = {}
    adjudicator = None
    if expected:
        if adjudication_path is None:
            raise ValueError("disagreements require a third human adjudicator")
        adjudicator = Adjudication.model_validate_json(adjudication_path.read_bytes())
        if (
            adjudicator.origin != "human"
            or adjudicator.reviewer_id in {a.reviewer_id, b.reviewer_id}
            or adjudicator.comparison_sha256 != digest_file(comparison)
        ):
            raise ValueError("independent adjudicator and original comparison required")
        resolutions = {
            (row.id, row.category): row.label for row in adjudicator.resolutions
        }
        if (
            len(resolutions) != len(adjudicator.resolutions)
            or set(resolutions) != expected
        ):
            raise ValueError("resolve exactly the disagreed categories")
    elif adjudication_path is not None:
        raise ValueError("no adjudication is needed for unanimous reviews")
    records = [
        {
            **row,
            "labels": {
                category: resolutions.get(
                    (row["id"], category), left[row["id"]].labels[category]
                )
                for category in sorted(categories(taxonomy, row["input_type"]))
            },
        }
        for row in cases
    ]
    output.mkdir(parents=True, exist_ok=False)
    write(output / "private-reviewed-labels.json", records)
    write(
        output / "provenance.json",
        {
            "packet_sha256": digest_file(directory / "protocol.json"),
            "labels_sha256": digest_file(output / "private-reviewed-labels.json"),
            "reviewers": [a.reviewer_id, b.reviewer_id],
            "adjudicator": adjudicator.reviewer_id if adjudicator else None,
            "review_sha256": [digest_file(first), digest_file(second)],
            "adjudication_sha256": digest_file(adjudication_path)
            if adjudication_path
            else None,
            "comparison_sha256": digest_file(comparison),
            "origin": protocol["origin"],
            "prior_use": protocol["prior_use"],
            "limitations": protocol["limitations"],
        },
    )


def export(
    directory: Path,
    reviewed: Path,
    category: str,
    model_category: str,
    mapping_note: str,
    output: Path,
) -> None:
    protocol, taxonomy, _ = packet(directory)
    model_category = PolicyRule(category=model_category, review_threshold=0.5).category
    if not mapping_note.strip():
        raise ValueError("a score-mapping rationale is required")
    if category not in taxonomy.categories:
        raise ValueError("unknown annotation category")
    provenance = json.loads((reviewed / "provenance.json").read_bytes())
    if provenance["packet_sha256"] != digest_file(
        directory / "protocol.json"
    ) or provenance["labels_sha256"] != digest_file(
        reviewed / "private-reviewed-labels.json"
    ):
        raise ValueError("finalized labels changed or belong to a different packet")
    records = json.loads((reviewed / "private-reviewed-labels.json").read_bytes())
    selected = [
        row
        for row in records
        if category in row["labels"] and row["labels"][category] != "uncertain"
    ]
    cases = [
        CalibrationCase.model_validate(
            {k: row[k] for k in ["id", "group", "input_type", "slice", "split"]}
            | (
                {
                    "expected": "block"
                    if row["labels"][category] == "present"
                    else "allow"
                }
                if row["split"] == "validation"
                else {}
            )
        )
        for row in selected
    ]
    validation, _ = partition_cases(cases)
    if {row.expected for row in validation} != {"allow", "block"}:
        raise ValueError("validation needs both present and absent cases")
    if {row["labels"][category] for row in selected if row["split"] == "test"} != {
        "present",
        "absent",
    }:
        raise ValueError("reserved test cases require both label classes")
    output.mkdir(parents=True, exist_ok=False)
    write(
        output / "cases.json",
        [row.model_dump(mode="json", exclude_none=True) for row in cases],
    )
    write(
        output / "export.json",
        {
            "packet_sha256": digest_file(directory / "protocol.json"),
            "reviewed_provenance_sha256": digest_file(reviewed / "provenance.json"),
            "cases_sha256": digest_file(output / "cases.json"),
            "category": category,
            "model_category": model_category,
            "mapping_note": mapping_note,
            "evaluation_scope": (
                "declared_unseen_requires_source_audit"
                if protocol["prior_use"] == "unseen_declared"
                and protocol["origin"] != "synthetic"
                else "diagnostic_only"
            ),
            "uncertain_excluded": sum(
                category in row["labels"] and row["labels"][category] == "uncertain"
                for row in records
            ),
            "origin": protocol["origin"],
            "prior_use": protocol["prior_use"],
            "scope": (
                "reviewed annotations, not a model recommendation; "
                "score semantics need independent validation"
            ),
        },
    )


def unseal(
    reviewed: Path,
    exported: Path,
    proposal_path: Path,
    validation_results: Path,
    output: Path,
) -> None:
    proposal = FrozenProposal.model_validate_json(proposal_path.read_bytes())
    validation_bytes = validation_results.read_bytes()
    metadata = json.loads((exported / "export.json").read_bytes())
    provenance_path = reviewed / "provenance.json"
    provenance = json.loads(provenance_path.read_bytes())
    if (
        metadata["reviewed_provenance_sha256"] != digest_file(provenance_path)
        or provenance["labels_sha256"]
        != digest_file(reviewed / "private-reviewed-labels.json")
        or metadata["cases_sha256"] != digest_file(exported / "cases.json")
        or proposal.inputs.cases_sha256 != metadata["cases_sha256"]
        or proposal.category != metadata["model_category"]
        or proposal.inputs.validation_results_sha256
        != hashlib.sha256(validation_bytes).hexdigest()
    ):
        raise ValueError("proposal/export/review fingerprints do not match")
    records = {
        row["id"]: row
        for row in json.loads((reviewed / "private-reviewed-labels.json").read_bytes())
    }
    cases = [
        CalibrationCase.model_validate(row)
        for row in json.loads((exported / "cases.json").read_bytes())
    ]
    observations = [
        Observation.model_validate_json(line) for line in validation_bytes.splitlines()
    ]
    recomputed = calibrate(cases, observations, proposal.category, proposal.budget)
    if (
        recomputed["status"] != "proposal"
        or recomputed["threshold"] != proposal.threshold
        or recomputed["provenance"] != proposal.provenance.model_dump(mode="json")
    ):
        raise ValueError("proposal does not match the original validation evidence")
    _, test = partition_cases(cases)
    write(
        output,
        [
            {
                "id": row.id,
                "expected": "block"
                if records[row.id]["labels"][metadata["category"]] == "present"
                else "allow",
            }
            for row in test
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("prepare", "bundle", "compare", "finalize", "export", "unseal"),
    )
    for name in (
        "source",
        "taxonomy",
        "packet",
        "first",
        "second",
        "comparison",
        "adjudication",
        "reviewed",
        "exported",
        "proposal",
        "validation-results",
        "output",
    ):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--reviewer")
    parser.add_argument("--category")
    parser.add_argument("--model-category")
    parser.add_argument("--mapping-note")
    args = parser.parse_args()
    requirements = {
        "prepare": ["source", "taxonomy", "output"],
        "bundle": ["packet", "reviewer", "output"],
        "compare": ["packet", "first", "second", "output"],
        "finalize": ["packet", "first", "second", "comparison", "output"],
        "export": [
            "packet",
            "reviewed",
            "category",
            "model_category",
            "mapping_note",
            "output",
        ],
        "unseal": ["reviewed", "exported", "proposal", "validation_results", "output"],
    }
    for name in requirements[args.command]:
        if getattr(args, name) is None:
            parser.error("missing --" + name.replace("_", "-"))
    if args.command == "prepare":
        prepare(args.source, args.taxonomy, args.output)
    elif args.command == "bundle":
        bundle(args.packet, args.reviewer, args.output)
    elif args.command == "compare":
        compare(args.packet, args.first, args.second, args.output)
    elif args.command == "finalize":
        finalize(
            args.packet,
            args.first,
            args.second,
            args.comparison,
            args.adjudication,
            args.output,
        )
    elif args.command == "export":
        export(
            args.packet,
            args.reviewed,
            args.category,
            args.model_category,
            args.mapping_note,
            args.output,
        )
    else:
        unseal(
            args.reviewed,
            args.exported,
            args.proposal,
            args.validation_results,
            args.output,
        )


if __name__ == "__main__":
    main()
