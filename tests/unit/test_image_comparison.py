import hashlib
from typing import Any

import pytest

from evaluation.assess import AssessmentObservation
from evaluation.compare_images import PROFILES, compare
from evaluation.report import Action, Case


def cohort() -> list[Case]:
    labels: tuple[Action, ...] = ("allow", "block")
    return [
        Case(
            id=label,
            group=label,
            split="test",
            input_type="image",
            slice="photo",
            expected=label,
        )
        for label in labels
    ]


def runs() -> dict[str, list[AssessmentObservation]]:
    return {
        name: [
            AssessmentObservation.model_validate(
                {
                    "id": case.id,
                    "result": {
                        "request_id": "req_"
                        + hashlib.md5(
                            (name + case.id).encode(), usedforsecurity=False
                        ).hexdigest(),
                        "input_type": "image",
                        "policy": {"id": name, "version": "1"},
                        "decision": {"action": "allow", "reasons": []},
                        "findings": [{"category": "nudity", "score": 0.1}],
                        "coverage": {
                            "evaluated": ["nudity"],
                            "unsupported": [],
                            "not_applicable": [],
                        },
                        "models": [{"id": name, "revision": "a" * 40}],
                        "preprocessing_version": "1",
                    },
                }
            )
            for case in cohort()
        ]
        for name in PROFILES
    }


def change(data: dict[str, list[AssessmentObservation]], **changes: Any) -> None:
    record = data["combined"][1].model_dump()
    record["result"].update(changes)
    data["combined"][1] = AssessmentObservation.model_validate(record)


def test_paired_improvement_and_false_alarm() -> None:
    data = runs()
    change(data, decision={"action": "review", "reasons": ["nudity"]})
    report = compare(cohort(), data)
    assert report["profiles"]["classifier"]["escalation"]["missed"] == 1
    assert report["profiles"]["combined"]["escalation"]["missed"] == 0
    assert report["paired"]["classifier"]["decisions_changed"] == 1


def test_failures_remain_in_denominator() -> None:
    data = runs()
    data["combined"][1] = AssessmentObservation(id="block", error="timeout")
    report = compare(cohort(), data)["profiles"]["combined"]
    assert report["overall"]["cases"] == 2
    assert report["overall"]["failed"] == 1
    assert report["escalation"]["failed_positive"] == 1


@pytest.mark.parametrize("field", ["policy", "models", "preprocessing_version"])
def test_rejects_changed_provenance(field: str) -> None:
    data = runs()
    values = {
        "policy": {"id": "other", "version": "2"},
        "models": [{"id": "other", "revision": "b" * 40}],
        "preprocessing_version": "2",
    }
    change(data, **{field: values[field]})
    with pytest.raises(ValueError, match="provenance changed"):
        compare(cohort(), data)


def test_rejects_missing_and_duplicate_cases() -> None:
    data = runs()
    data["detector"] = [data["detector"][0]] * 2
    with pytest.raises(ValueError, match="IDs exactly"):
        compare(cohort(), data)


def test_rejects_reused_requests() -> None:
    data = runs()
    change(data, request_id=data["classifier"][0].result.request_id)  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="distinct"):
        compare(cohort(), data)


def test_unsupported_is_failure() -> None:
    data = runs()
    change(
        data,
        coverage={
            "evaluated": ["nudity"],
            "unsupported": ["other"],
            "not_applicable": [],
        },
    )
    assert compare(cohort(), data)["profiles"]["combined"]["overall"]["failed"] == 1


def test_cli_hashes_inputs_and_refuses_overwrite(
    tmp_path: Any, monkeypatch: Any
) -> None:
    import json
    import sys

    from evaluation.compare_images import main

    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([case.model_dump() for case in cohort()]))
    arguments = ["compare_images", "--cases", str(cases_path)]
    for name, observations in runs().items():
        path = tmp_path / f"{name}.jsonl"
        path.write_text("\n".join(o.model_dump_json() for o in observations) + "\n")
        arguments.extend([f"--{name}", str(path)])
    output = tmp_path / "report.json"
    arguments.extend(["--output", str(output)])
    monkeypatch.setattr(sys, "argv", arguments)
    main()
    report = json.loads(output.read_text())
    assert (
        report["inputs_sha256"]["cases"]
        == hashlib.sha256(cases_path.read_bytes()).hexdigest()
    )
    assert report["profiles"]["combined"]["overall"]["cases"] == 2
    with pytest.raises(FileExistsError):
        main()


def test_text_study_preparation_ignores_image_candidates(
    tmp_path: Any, monkeypatch: Any
) -> None:
    import sys

    from evaluation import compare_text

    prepared: list[str] = []

    def capture(models: Any, *, threads: int, offline: bool) -> None:
        assert models.text is not None
        assert not models.configured_images
        assert threads == 2 and not offline
        prepared.append(models.text.id)

    monkeypatch.setattr(compare_text, "prepare_models", capture)
    monkeypatch.setattr(
        sys, "argv", ["compare_text", "models", "--cache", str(tmp_path)]
    )
    compare_text.main()
    assert prepared == [
        "gravitee-io/distilbert-multilingual-toxicity-classifier",
        "Horizon-Labs/multilingual-toxicity-small",
    ]
