import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evaluation.calibrate import (
    Budget,
    CalibrationCase,
    ModelProvenance,
    Observation,
    partition_cases,
)
from evaluation.compare_text import digest_file
from evaluation.report import Name
from modera.policies import Policy, PolicyRule, evaluate_policy
from modera.results import Category, ModerationResult, Score

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Fingerprints(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cases_sha256: Digest
    validation_results_sha256: Digest


class FrozenProposal(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)
    status: Literal["proposal"]
    threshold: Score
    category: Category
    policy_rule: PolicyRule
    budget: Budget
    provenance: ModelProvenance
    inputs: Fingerprints
    calibrator_sha256: Digest

    @model_validator(mode="after")
    def validate_rule(self) -> Self:
        rule = self.policy_rule
        if (
            rule.category != self.category
            or rule.review_threshold != self.threshold
            or rule.block_threshold is not None
            or not rule.required
        ):
            raise ValueError("proposal rule does not match the frozen review cutoff")
        return self


class HeldOutLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Name
    expected: Literal["allow", "block"]


class AssessmentObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Name
    result: ModerationResult | None = None
    error: Name | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if (self.result is None) == (self.error is None):
            raise ValueError("provide exactly one result or error")
        return self


def assessment_metrics(rows: list[tuple[bool, bool | None]]) -> dict[str, Any]:
    positives = sum(label for label, _ in rows)
    negatives = len(rows) - positives
    tp = sum(label and predicted is True for label, predicted in rows)
    fp = sum(not label and predicted is True for label, predicted in rows)
    fn = sum(label and predicted is False for label, predicted in rows)
    tn = sum(not label and predicted is False for label, predicted in rows)
    completed = tp + fp + fn + tn
    return {
        "cases": len(rows),
        "positive_cases": positives,
        "negative_cases": negatives,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "positive_failed": positives - tp - fn,
        "negative_failed": negatives - fp - tn,
        "completed": completed,
        "failed": len(rows) - completed,
        "completion_rate": completed / len(rows) if rows else None,
        "recall_completed": tp / (tp + fn) if tp + fn else None,
        "recall_all_cases": tp / positives if positives else None,
        "false_positive_rate_completed": fp / (fp + tn) if fp + tn else None,
    }


def assess(
    cases: list[CalibrationCase],
    labels: list[HeldOutLabel],
    observations: list[AssessmentObservation],
    proposal: FrozenProposal,
    validation_observations: list[Observation],
) -> dict[str, Any]:
    validation, held_out = partition_cases(cases)
    validation_ids = {row.id for row in validation_observations}
    if len(validation_ids) != len(validation_observations) or validation_ids != {
        case.id for case in validation
    }:
        raise ValueError("validation results must match original IDs exactly once")
    validation_requests = {row.result.request_id for row in validation_observations}
    if len(validation_requests) != len(validation_observations):
        raise ValueError("validation results must have distinct request IDs")
    if any(
        row.result.fallback
        or ModelProvenance.from_result(row.result) != proposal.provenance
        for row in validation_observations
    ):
        raise ValueError("original validation provenance differs from proposal")
    if any(case.input_type != proposal.provenance.input_type for case in cases):
        raise ValueError("manifest modality differs from the proposal")
    expected = {row.id: row.expected for row in labels}
    observed = {row.id: row for row in observations}
    reserved = {case.id for case in held_out}
    if len(expected) != len(labels) or set(expected) != reserved:
        raise ValueError("labels must match the reserved test IDs exactly once")
    if set(expected.values()) != {"allow", "block"}:
        raise ValueError("held-out labels require both positive and negative classes")
    if len(observed) != len(observations) or set(observed) != reserved:
        raise ValueError("results must match the reserved test IDs exactly once")
    responses = [row.result for row in observations if row.result is not None]
    if len({result.request_id for result in responses}) != len(responses):
        raise ValueError("test results must have distinct request IDs")
    if validation_requests & {result.request_id for result in responses}:
        raise ValueError("validation and test request IDs must not overlap")
    policy = Policy(
        id="assessment",
        version="1",
        text=(proposal.policy_rule,)
        if proposal.provenance.input_type == "text"
        else (),
        image=(proposal.policy_rule,)
        if proposal.provenance.input_type == "image"
        else (),
    )
    rows = []
    slices: dict[str, list[tuple[bool, bool | None]]] = defaultdict(list)
    errors: Counter[str] = Counter()
    for case in held_out:
        observation = observed[case.id]
        result = observation.result
        predicted: bool | None = None
        error = observation.error
        if result is not None:
            if result.input_type != case.input_type:
                raise ValueError("result modality differs from the reserved case")
            if result.fallback:
                error = "fallback_result"
            else:
                findings = [
                    finding
                    for finding in result.findings
                    if finding.category == proposal.category
                ]
                if not findings or findings[0].source != "model":
                    error = "model_check_unavailable"
                else:
                    if ModelProvenance.from_result(result) != proposal.provenance:
                        raise ValueError(
                            "test model/preprocessing differs from calibration"
                        )
                    decision = evaluate_policy(
                        policy, case.input_type, result.findings, result.coverage
                    )
                    predicted = decision.action != "allow"
        if error is not None:
            errors[error] += 1
        row = (expected[case.id] == "block", predicted)
        rows.append(row)
        slices[case.slice].append(row)
    metrics = assessment_metrics(rows)
    fpr, recall = metrics["false_positive_rate_completed"], metrics["recall_all_cases"]
    passed = (
        not metrics["failed"]
        and fpr is not None
        and recall is not None
        and fpr <= proposal.budget.max_false_positive_rate
        and recall >= proposal.budget.min_recall
    )
    return {
        "status": "incomplete"
        if metrics["failed"]
        else "passed"
        if passed
        else "failed",
        "targets_met": passed,
        "threshold": proposal.threshold,
        "category": proposal.category,
        "assessed_rule": proposal.policy_rule.model_dump(
            mode="json", exclude_none=True
        ),
        "budget": proposal.budget.model_dump(),
        "provenance": proposal.provenance.model_dump(mode="json"),
        "metrics": metrics,
        "errors": dict(errors),
        "test_groups": len({case.group for case in held_out}),
        "test_slices": {
            key: assessment_metrics(value) for key, value in sorted(slices.items())
        },
        "limitations": (
            "Empirical held-out assessment of one category review rule, not a complete "
            "policy or a production guarantee. Failed checks are not clean decisions. "
            "No threshold is selected here. File hashes cannot establish that test "
            "labels were unseen or that sources are independent."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assess a frozen review proposal without retuning"
    )
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--validation-results", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--test-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        data = {
            key: path.read_bytes()
            for key, path in {
                "proposal": args.proposal,
                "cases": args.cases,
                "labels": args.labels,
                "validation_results": args.validation_results,
                "test_results": args.test_results,
            }.items()
        }
        proposal = FrozenProposal.model_validate_json(data["proposal"])
        if hashlib.sha256(data["cases"]).hexdigest() != proposal.inputs.cases_sha256:
            raise ValueError("manifest changed after calibration")
        if (
            hashlib.sha256(data["validation_results"]).hexdigest()
            != proposal.inputs.validation_results_sha256
        ):
            raise ValueError("validation results changed after calibration")
        validation_observations = [
            Observation.model_validate_json(line)
            for line in data["validation_results"].splitlines()
        ]
        cases = [
            CalibrationCase.model_validate(row) for row in json.loads(data["cases"])
        ]
        labels = [
            HeldOutLabel.model_validate(row) for row in json.loads(data["labels"])
        ]
        observations = [
            AssessmentObservation.model_validate_json(line)
            for line in data["test_results"].splitlines()
        ]
        report = assess(cases, labels, observations, proposal, validation_observations)
        report["inputs"] = {
            key + "_sha256": hashlib.sha256(value).hexdigest()
            for key, value in data.items()
        }
        report["calibrator_sha256"] = proposal.calibrator_sha256
        report["assessor_sha256"] = digest_file(Path(__file__))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as output:
            output.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"Assessment failed: {exc}\n")
    if report["status"] != "passed":
        parser.exit(
            2, "The frozen proposal did not pass complete held-out assessment.\n"
        )


if __name__ == "__main__":
    main()
