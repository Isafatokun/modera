import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evaluation.calibrate import (
    Budget,
    CalibrationCase,
    Observation,
    calibrate,
    main,
    select_cutoff,
)
from modera.policies import Policy, PolicyRule, evaluate_policy
from modera.results import Coverage, Finding

BUDGET = Budget(max_false_positive_rate=0.0, min_recall=1.0)


def case(identifier: str, **changes: object) -> CalibrationCase:
    return CalibrationCase.model_validate(
        {
            "id": identifier,
            "group": identifier,
            "split": "validation",
            "input_type": "text",
            "slice": "default",
            "expected": "block",
            **changes,
        }
    )


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


@pytest.fixture
def dataset() -> tuple[list[CalibrationCase], list[Observation]]:
    return (
        [
            case("positive"),
            case("negative", expected="allow"),
            case("reserved", split="test", expected=None),
        ],
        [observation("positive", 0.8), observation("negative", 0.2)],
    )


def test_threshold_matches_production_inclusive_comparison(
    dataset: tuple[list[CalibrationCase], list[Observation]],
) -> None:
    cases, results = dataset
    proposal = calibrate(cases, results, "toxicity", BUDGET)
    threshold = proposal["threshold"]
    assert threshold == math.nextafter(0.2, 1.0)
    rule = PolicyRule.model_validate_json(json.dumps(proposal["policy_rule"]))
    policy = Policy(id="calibrated", version="1", text=(rule,))
    for score, action in [(0.2, "allow"), (threshold, "review"), (0.8, "review")]:
        result = evaluate_policy(
            policy,
            "text",
            [Finding(category="toxicity", score=score)],
            Coverage(evaluated=["toxicity"], unsupported=[], not_applicable=[]),
        )
        assert result.action == action
    assert proposal["validation_metrics"]["recall"] == 1
    assert proposal["validation_metrics"]["false_positive_rate"] == 0
    assert proposal["held_out_cases_reserved"] == 1


@pytest.mark.parametrize(
    "rows",
    [
        [(True, 0.5), (False, 0.5)],
        [(True, 1.0), (False, 1.0)],
        [(True, 0.2), (False, 0.9)],
    ],
)
def test_no_candidate_when_budgets_cannot_be_met(
    rows: list[tuple[bool, float]],
) -> None:
    threshold, _ = select_cutoff(rows, BUDGET)
    assert threshold is None


def test_lowest_feasible_cutoff_maximizes_recall() -> None:
    threshold, metrics = select_cutoff(
        [(True, 0.5), (True, 0.9), (False, 0.2), (False, 0.7)],
        Budget(max_false_positive_rate=0.5, min_recall=1.0),
    )
    assert threshold == math.nextafter(0.2, 1.0)
    assert metrics is not None and metrics["recall"] == 1


def test_budget_at_one_allows_zero_threshold() -> None:
    threshold, _ = select_cutoff(
        [(True, 0.0), (False, 1.0)], Budget(max_false_positive_rate=1.0, min_recall=1.0)
    )
    assert threshold == 0


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [(True, 0.4)],
        [(False, 0.5)],
        [(True, float("nan")), (False, 0.1)],
        [(True, 1.1), (False, 0.2)],
    ],
)
def test_invalid_or_single_class_validation_is_rejected(
    rows: list[tuple[bool, float]],
) -> None:
    with pytest.raises(ValueError):
        select_cutoff(rows, BUDGET)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1])
def test_budget_is_finite_and_bounded(value: float) -> None:
    with pytest.raises(ValidationError):
        Budget(max_false_positive_rate=value, min_recall=1.0)


def test_reserved_test_labels_and_scores_are_rejected(
    dataset: tuple[list[CalibrationCase], list[Observation]],
) -> None:
    cases, results = dataset
    with pytest.raises(ValidationError, match="held-out labels"):
        case("reserved", split="test", expected="allow")
    with pytest.raises(ValueError, match="no test results"):
        calibrate(cases, [*results, observation("reserved", 0.5)], "toxicity", BUDGET)


@pytest.mark.parametrize(
    "change",
    [
        "missing_test",
        "missing_validation",
        "duplicate_id",
        "overlapping_group",
        "mixed_modality",
    ],
)
def test_manifest_separation_is_enforced(
    dataset: tuple[list[CalibrationCase], list[Observation]], change: str
) -> None:
    cases, results = dataset
    if change == "missing_test":
        cases = cases[:2]
    elif change == "missing_validation":
        cases = cases[2:]
    elif change == "duplicate_id":
        cases = [*cases, cases[0]]
    elif change == "overlapping_group":
        cases[2] = case("reserved", split="test", expected=None, group="positive")
    else:
        cases[2] = case("reserved", split="test", expected=None, input_type="image")
    with pytest.raises(ValueError):
        calibrate(cases, results, "toxicity", BUDGET)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "revision",
        "preprocessing",
        "request_id",
        "fallback",
        "modality",
        "missing_category",
        "rule_score",
    ],
)
def test_failed_mixed_or_incomplete_results_are_not_silently_used(
    dataset: tuple[list[CalibrationCase], list[Observation]], change: str
) -> None:
    cases, results = dataset
    if change == "missing":
        results = results[:1]
    elif change == "duplicate":
        results = [*results, results[0]]
    elif change == "revision":
        results[1] = observation(
            "negative", 0.2, models=[{"id": "example/classifier", "revision": "b" * 40}]
        )
    elif change == "preprocessing":
        results[1] = observation("negative", 0.2, preprocessing_version="other")
    elif change == "request_id":
        results[1] = observation(
            "negative", 0.2, request_id=results[0].result.request_id
        )
    elif change == "fallback":
        results[1] = observation("negative", 0.2, fallback=True, rules_version="1")
    elif change == "modality":
        results[1] = observation("negative", 0.2, input_type="image")
    elif change == "missing_category":
        results[1] = observation(
            "negative",
            0.2,
            findings=[{"category": "other", "score": 0.2}],
            coverage={"evaluated": ["other"], "unsupported": [], "not_applicable": []},
        )
    else:
        results = [
            observation(
                "positive",
                0.8,
                models=[],
                rules_version="1",
                findings=[{"category": "toxicity", "score": 0.8, "source": "rules"}],
            ),
            results[1],
        ]
    with pytest.raises(ValueError):
        calibrate(cases, results, "toxicity", BUDGET)


def test_held_out_metadata_does_not_change_cutoff(
    dataset: tuple[list[CalibrationCase], list[Observation]],
) -> None:
    cases, results = dataset
    before = calibrate(cases, results, "toxicity", BUDGET)
    cases[2] = case("new-reserved", split="test", expected=None, slice="different")
    after = calibrate(cases, results, "toxicity", BUDGET)
    assert before["threshold"] == after["threshold"]
    assert before["validation_metrics"] == after["validation_metrics"]


def test_cli_preserves_existing_outputs(
    dataset: tuple[list[CalibrationCase], list[Observation]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    cases, results = dataset
    manifest, observations, output = (
        tmp_path / "cases.json",
        tmp_path / "results.jsonl",
        tmp_path / "output" / "proposal.json",
    )
    manifest.write_text(
        json.dumps([row.model_dump(exclude_none=True) for row in cases])
    )
    observations.write_text("\n".join(row.model_dump_json() for row in results) + "\n")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibrate",
            "--cases",
            str(manifest),
            "--validation-results",
            str(observations),
            "--category",
            "toxicity",
            "--max-false-positive-rate",
            "0",
            "--min-recall",
            "1",
            "--output",
            str(output),
        ],
    )
    main()
    original = output.read_bytes()
    assert json.loads(original)["status"] == "proposal"
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert output.read_bytes() == original


def test_proposal_exposes_slice_rates_without_claiming_missing_classes(
    dataset: tuple[list[CalibrationCase], list[Observation]],
) -> None:
    cases, results = dataset
    cases[0] = case("positive", slice="positives-only")
    cases[1] = case("negative", expected="allow", slice="negatives-only")
    proposal = calibrate(cases, results, "toxicity", BUDGET)
    assert (
        proposal["validation_slices"]["positives-only"]["false_positive_rate"] is None
    )
    assert proposal["validation_slices"]["negatives-only"]["recall"] is None


def test_image_model_category_uses_the_same_calibration_contract(
    dataset: tuple[list[CalibrationCase], list[Observation]],
) -> None:
    cases, _ = dataset
    cases = [row.model_copy(update={"input_type": "image"}) for row in cases]
    results = [
        observation(
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
        for identifier, score in [("positive", 0.8), ("negative", 0.2)]
    ]
    proposal = calibrate(cases, results, "classifier.porn", BUDGET)
    assert proposal["policy_rule"]["category"] == "classifier.porn"
    assert proposal["provenance"]["input_type"] == "image"


@pytest.mark.parametrize(
    "changes",
    [
        {"expected": None},
        {"expected": "review"},
        {"split": "diagnostic"},
    ],
)
def test_invalid_validation_labels_and_splits_are_rejected(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        case("invalid", **changes)


def test_cli_records_infeasibility_without_emitting_policy_rule(
    dataset: tuple[list[CalibrationCase], list[Observation]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    cases, _ = dataset
    results = [observation("positive", 0.1), observation("negative", 0.9)]
    manifest, observed, output = (
        tmp_path / "cases.json",
        tmp_path / "results.jsonl",
        tmp_path / "proposal.json",
    )
    manifest.write_text(
        json.dumps([row.model_dump(exclude_none=True) for row in cases])
    )
    observed.write_text("\n".join(row.model_dump_json() for row in results) + "\n")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibrate",
            "--cases",
            str(manifest),
            "--validation-results",
            str(observed),
            "--category",
            "toxicity",
            "--max-false-positive-rate",
            "0",
            "--min-recall",
            "1",
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    report = json.loads(output.read_bytes())
    assert report["status"] == "no_candidate"
    assert report["threshold"] is None
    assert report["policy_rule"] is None


def test_fingerprints_bind_to_consumed_bytes_when_source_changes(
    dataset: tuple[list[CalibrationCase], list[Observation]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import sys

    module = importlib.import_module("evaluation.calibrate")
    original_calibrate = module.calibrate
    cases, results = dataset
    manifest, observed, output = (
        tmp_path / "cases.json",
        tmp_path / "results.jsonl",
        tmp_path / "proposal.json",
    )
    case_data = json.dumps(
        [row.model_dump(exclude_none=True) for row in cases]
    ).encode()
    result_data = ("\n".join(row.model_dump_json() for row in results) + "\n").encode()
    manifest.write_bytes(case_data)
    observed.write_bytes(result_data)

    def change_sources(*args: Any, **kwargs: Any) -> dict[str, Any]:
        proposal: dict[str, Any] = original_calibrate(*args, **kwargs)
        manifest.write_bytes(b"changed")
        observed.write_bytes(b"changed")
        return proposal

    monkeypatch.setattr(module, "calibrate", change_sources)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibrate",
            "--cases",
            str(manifest),
            "--validation-results",
            str(observed),
            "--category",
            "toxicity",
            "--max-false-positive-rate",
            "0",
            "--min-recall",
            "1",
            "--output",
            str(output),
        ],
    )
    main()
    report = json.loads(output.read_bytes())
    assert report["inputs"]["cases_sha256"] == hashlib.sha256(case_data).hexdigest()
    assert (
        report["inputs"]["validation_results_sha256"]
        == hashlib.sha256(result_data).hexdigest()
    )
