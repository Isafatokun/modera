import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evaluation.assess import (
    AssessmentObservation,
    FrozenProposal,
    HeldOutLabel,
    assess,
    assessment_metrics,
    main,
)
from evaluation.calibrate import Budget, CalibrationCase, Observation, calibrate


def observation(identifier: str, score: float, **changes: Any) -> Observation:
    return Observation.model_validate(
        {
            "id": identifier,
            "result": {
                "request_id": "req_"
                + hashlib.sha256(identifier.encode()).hexdigest()[:32],
                "input_type": "text",
                "policy": {"id": "original", "version": "1"},
                "decision": {"action": "allow", "reasons": []},
                "findings": [{"category": "toxicity", "score": score}],
                "coverage": {
                    "evaluated": ["toxicity"],
                    "unsupported": [],
                    "not_applicable": [],
                },
                "models": [{"id": "example/classifier", "revision": "a" * 40}],
                "preprocessing_version": "fixture-1",
                **changes,
            },
        }
    )


@dataclass
class Study:
    cases: list[CalibrationCase]
    validation: list[Observation]
    labels: list[HeldOutLabel]
    observations: list[AssessmentObservation]
    proposal: FrozenProposal

    def run(self) -> dict[str, Any]:
        return assess(
            self.cases, self.labels, self.observations, self.proposal, self.validation
        )

    def result(self, index: int, score: float, **changes: Any) -> None:
        identifier = self.observations[index].id
        self.observations[index] = AssessmentObservation.model_validate(
            observation(identifier, score, **changes).model_dump()
        )


@pytest.fixture
def study() -> Study:
    cases = [
        CalibrationCase.model_validate(
            {
                "id": identifier,
                "group": identifier,
                "split": split,
                "input_type": "text",
                "slice": label,
                **({"expected": label} if split == "validation" else {}),
            }
        )
        for identifier, split, label in [
            ("v1", "validation", "block"),
            ("v2", "validation", "allow"),
            ("t1", "test", "block"),
            ("t2", "test", "allow"),
        ]
    ]
    validation = [observation("v1", 0.8), observation("v2", 0.2)]
    report = calibrate(
        cases,
        validation,
        "toxicity",
        Budget(max_false_positive_rate=0.0, min_recall=1.0),
    )
    report.update(
        inputs={"cases_sha256": "a" * 64, "validation_results_sha256": "b" * 64},
        calibrator_sha256="c" * 64,
    )
    return Study(
        cases,
        validation,
        [
            HeldOutLabel(id="t1", expected="block"),
            HeldOutLabel(id="t2", expected="allow"),
        ],
        [
            AssessmentObservation.model_validate(
                observation(identifier, score).model_dump()
            )
            for identifier, score in [("t1", 0.9), ("t2", 0.1)]
        ],
        FrozenProposal.model_validate(report),
    )


def test_assessment_uses_native_rule_and_ignores_original_decisions(
    study: Study,
) -> None:
    study.result(0, study.proposal.threshold)
    report = study.run()
    assert report["status"] == "passed"
    assert report["metrics"]["true_positive"] == 1
    assert report["threshold"] == study.proposal.threshold
    assert report["test_slices"]["block"]["false_positive_rate_completed"] is None
    assert report["test_slices"]["allow"]["recall_completed"] is None


@pytest.mark.parametrize("index,score", [(0, 0.15), (1, 0.7)])
def test_test_failures_never_retune_threshold(
    study: Study, index: int, score: float
) -> None:
    study.result(index, score)
    report = study.run()
    assert report["status"] == "failed"
    assert report["targets_met"] is False
    assert report["threshold"] == study.proposal.threshold
    assert report["assessed_rule"] == study.proposal.policy_rule.model_dump(
        exclude_none=True
    )


@pytest.mark.parametrize("index", [0, 1])
def test_errors_are_incomplete_even_when_completed_subset_is_perfect(
    study: Study, index: int
) -> None:
    study.observations[index] = AssessmentObservation(
        id=study.observations[index].id, error="check_unavailable"
    )
    report = study.run()
    assert report["status"] == "incomplete"
    assert report["targets_met"] is False
    assert report["metrics"]["completion_rate"] == 0.5
    assert (
        report["metrics"]["positive_failed" if index == 0 else "negative_failed"] == 1
    )
    assert report["errors"] == {"check_unavailable": 1}


@pytest.mark.parametrize("change", ["fallback", "missing", "rules"])
def test_unavailable_model_checks_are_never_clean(study: Study, change: str) -> None:
    if change == "fallback":
        study.result(0, 0.9, fallback=True, rules_version="1")
    elif change == "missing":
        study.result(
            0,
            0.9,
            findings=[{"category": "other", "score": 0.9}],
            coverage={"evaluated": ["other"], "unsupported": [], "not_applicable": []},
        )
    else:
        study.result(
            0,
            0.9,
            findings=[{"category": "toxicity", "score": 0.9, "source": "rules"}],
            rules_version="1",
        )
    report = study.run()
    assert report["status"] == "incomplete"
    assert report["metrics"]["positive_failed"] == 1
    assert report["metrics"]["false_negative"] == 0


@pytest.mark.parametrize(
    "change",
    ["revision", "preprocessing", "modality", "request_id", "reuse_validation"],
)
def test_inconsistent_or_reused_test_responses_are_rejected(
    study: Study, change: str
) -> None:
    changes: dict[str, Any] = {}
    if change == "revision":
        changes["models"] = [{"id": "example/classifier", "revision": "b" * 40}]
    elif change == "preprocessing":
        changes["preprocessing_version"] = "other"
    elif change == "modality":
        changes["input_type"] = "image"
    elif change == "request_id":
        assert study.observations[1].result is not None
        changes["request_id"] = study.observations[1].result.request_id
    else:
        changes["request_id"] = study.validation[0].result.request_id
    study.result(0, 0.9, **changes)
    with pytest.raises(ValueError):
        study.run()


@pytest.mark.parametrize(
    "change",
    [
        "missing_label",
        "duplicate_label",
        "validation_label",
        "one_class",
        "missing_result",
        "duplicate_result",
        "validation_result",
        "missing_validation",
        "duplicate_validation",
        "validation_provenance",
    ],
)
def test_exact_separate_sets_are_required(study: Study, change: str) -> None:
    if change == "missing_label":
        study.labels.pop()
    elif change == "duplicate_label":
        study.labels.append(study.labels[0])
    elif change == "validation_label":
        study.labels[0] = HeldOutLabel(id="v1", expected="block")
    elif change == "one_class":
        study.labels[1] = HeldOutLabel(id="t2", expected="block")
    elif change == "missing_result":
        study.observations.pop()
    elif change == "duplicate_result":
        study.observations.append(study.observations[0])
    elif change == "validation_result":
        study.observations[0] = AssessmentObservation.model_validate(
            study.validation[0].model_dump()
        )
    elif change == "missing_validation":
        study.validation.pop()
    elif change == "duplicate_validation":
        study.validation.append(study.validation[0])
    else:
        study.validation[0] = observation("v1", 0.8, preprocessing_version="other")
    with pytest.raises(ValueError):
        study.run()


@pytest.mark.parametrize(
    "change", ["status", "threshold", "category", "block", "optional", "hash"]
)
def test_inconsistent_proposals_are_rejected(study: Study, change: str) -> None:
    data = study.proposal.model_dump()
    if change == "status":
        data["status"] = "no_candidate"
    elif change == "threshold":
        data["threshold"] = 0.6
    elif change == "category":
        data["category"] = "other"
    elif change == "block":
        data["policy_rule"]["block_threshold"] = 0.9
    elif change == "optional":
        data["policy_rule"]["required"] = False
    else:
        data["inputs"]["cases_sha256"] = "invalid"
    with pytest.raises(ValueError):
        FrozenProposal.model_validate(data)


@pytest.mark.parametrize(
    "data", [{"id": "t1"}, {"id": "t1", "error": "unavailable", "result": {}}]
)
def test_observation_requires_exactly_one_outcome(data: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        AssessmentObservation.model_validate(data)


def test_metrics_separate_all_failed_cases() -> None:
    metrics = assessment_metrics([(True, None), (False, None)])
    assert metrics["recall_completed"] is None
    assert metrics["false_positive_rate_completed"] is None
    assert metrics["recall_all_cases"] == 0
    assert metrics["failed"] == 2


def test_image_assessment_uses_same_rule_contract(study: Study) -> None:
    study.cases = [
        row.model_copy(update={"input_type": "image"}) for row in study.cases
    ]

    def image(identifier: str, score: float) -> Observation:
        return observation(
            identifier,
            score,
            input_type="image",
            findings=[{"category": "classifier.porn", "score": score}],
            coverage={
                "evaluated": ["classifier.porn"],
                "unsupported": [],
                "not_applicable": [],
            },
        )

    study.validation = [image("v1", 0.8), image("v2", 0.2)]
    study.observations = [
        AssessmentObservation.model_validate(image(identifier, score).model_dump())
        for identifier, score in [("t1", 0.9), ("t2", 0.1)]
    ]
    data = study.proposal.model_dump()
    data.update(
        category="classifier.porn",
        policy_rule={**data["policy_rule"], "category": "classifier.porn"},
        provenance={**data["provenance"], "input_type": "image"},
    )
    study.proposal = FrozenProposal.model_validate(data)
    assert study.run()["status"] == "passed"


def cli_files(
    study: Study, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    files = {
        key: tmp_path / f"{key}.json"
        for key in [
            "cases",
            "validation_results",
            "labels",
            "test_results",
            "proposal",
            "output",
        ]
    }
    files["cases"].write_text(
        json.dumps([row.model_dump(exclude_none=True) for row in study.cases])
    )
    files["validation_results"].write_text(
        "\n".join(row.model_dump_json() for row in study.validation) + "\n"
    )
    files["labels"].write_text(json.dumps([row.model_dump() for row in study.labels]))
    files["test_results"].write_text(
        "\n".join(row.model_dump_json() for row in study.observations) + "\n"
    )
    from evaluation.calibrate import main as calibrate_main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibrate",
            "--cases",
            str(files["cases"]),
            "--validation-results",
            str(files["validation_results"]),
            "--category",
            "toxicity",
            "--max-false-positive-rate",
            "0",
            "--min-recall",
            "1",
            "--output",
            str(files["proposal"]),
        ],
    )
    calibrate_main()
    args = ["assess"]
    for key, path in files.items():
        args.extend(["--" + key.replace("_", "-"), str(path)])
    monkeypatch.setattr(sys, "argv", args)
    return files


def test_cli_calibration_round_trip_and_no_overwrite(
    study: Study, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = cli_files(study, tmp_path, monkeypatch)
    main()
    data = files["output"].read_bytes()
    assert json.loads(data)["status"] == "passed"
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert files["output"].read_bytes() == data


@pytest.mark.parametrize("key", ["cases", "validation_results"])
def test_cli_rejects_changed_original_sources(
    study: Study, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    files = cli_files(study, tmp_path, monkeypatch)
    files[key].write_bytes(files[key].read_bytes() + b"\n")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert not files["output"].exists()


@pytest.mark.parametrize("failure", ["failed", "incomplete"])
def test_cli_reports_unmet_targets_with_exit_two(
    study: Study, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "failed":
        study.result(0, 0.1)
    else:
        study.observations[0] = AssessmentObservation(
            id="t1", error="check_unavailable"
        )
    files = cli_files(study, tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert json.loads(files["output"].read_bytes())["status"] == failure


def test_cli_hashes_consumed_bytes_even_if_sources_change(
    study: Study, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evaluation.assess as module

    files = cli_files(study, tmp_path, monkeypatch)
    original_data = {
        key: path.read_bytes() for key, path in files.items() if key != "output"
    }
    original_assess = module.assess

    def change_sources(*args: Any, **kwargs: Any) -> dict[str, Any]:
        report = original_assess(*args, **kwargs)
        for key in original_data:
            files[key].write_bytes(b"changed")
        return report

    monkeypatch.setattr(module, "assess", change_sources)
    main()
    report = json.loads(files["output"].read_bytes())
    assert report["inputs"] == {
        key + "_sha256": hashlib.sha256(data).hexdigest()
        for key, data in original_data.items()
    }
