import argparse
import hashlib
import json
import math
from collections import defaultdict
from itertools import groupby
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evaluation.compare_text import digest_file
from evaluation.report import Name
from modera.policies import InputType, PolicyRule
from modera.results import ModelVersion, ModerationResult, Score, Version


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Name
    result: ModerationResult


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_false_positive_rate: Score
    min_recall: Score


class CalibrationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Name
    group: Name
    split: Literal["validation", "test"]
    input_type: InputType
    slice: Name
    expected: Literal["allow", "block"] | None = None

    @model_validator(mode="after")
    def validate_label_access(self) -> Self:
        if self.split == "validation" and self.expected is None:
            raise ValueError("validation cases require labels")
        if self.split == "test" and self.expected is not None:
            raise ValueError("held-out labels must remain outside calibration")
        return self


class ModelProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    input_type: InputType
    models: list[ModelVersion] = Field(min_length=1)
    preprocessing_version: Version
    rules_version: Version | None

    @field_validator("models")
    @classmethod
    def order_models(cls, models: list[ModelVersion]) -> list[ModelVersion]:
        if len({model.id for model in models}) != len(models):
            raise ValueError("model identifiers must be unique")
        return sorted(models, key=lambda model: model.id)

    @classmethod
    def from_result(cls, result: ModerationResult) -> Self:
        return cls(
            input_type=result.input_type,
            models=result.models,
            preprocessing_version=result.preprocessing_version,
            rules_version=result.rules_version,
        )


def partition_cases(
    cases: list[CalibrationCase],
) -> tuple[list[CalibrationCase], list[CalibrationCase]]:
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("manifest must be nonempty with unique IDs")
    validation = [case for case in cases if case.split == "validation"]
    held_out = [case for case in cases if case.split == "test"]
    if not validation or not held_out:
        raise ValueError("both validation and held-out test cases are required")
    if {case.group for case in validation} & {case.group for case in held_out}:
        raise ValueError("source groups must not cross validation and test sets")
    if len({case.input_type for case in cases}) != 1:
        raise ValueError("calibrate one modality at a time")
    return validation, held_out


def select_cutoff(
    rows: list[tuple[bool, float]], budget: Budget
) -> tuple[float | None, dict[str, int | float] | None]:
    if any(not math.isfinite(score) or not 0 <= score <= 1 for _, score in rows):
        raise ValueError("scores must be finite and between zero and one")
    positives = sum(label for label, _ in rows)
    negatives = len(rows) - positives
    if not positives or not negatives:
        raise ValueError("validation requires both positive and negative examples")
    tp, fp = positives, negatives

    def metrics() -> dict[str, int | float]:
        return {
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": positives - tp,
            "true_negative": negatives - fp,
            "recall": tp / positives,
            "false_positive_rate": fp / negatives,
        }

    if fp / negatives <= budget.max_false_positive_rate:
        return 0.0, metrics()
    ordered = sorted(rows, key=lambda row: row[1])
    for score, tied in groupby(ordered, key=lambda row: row[1]):
        if score == 1:
            break
        labels = [label for label, _ in tied]
        positive_count = sum(labels)
        tp -= positive_count
        fp -= len(labels) - positive_count
        if fp / negatives <= budget.max_false_positive_rate:
            return (
                math.nextafter(score, 1.0)
                if tp / positives >= budget.min_recall
                else None,
                metrics(),
            )
    return None, None


def slice_metrics(rows: list[tuple[bool, float]], threshold: float) -> dict[str, Any]:
    positives = sum(label for label, _ in rows)
    negatives = len(rows) - positives
    tp = sum(label and score >= threshold for label, score in rows)
    fp = sum(not label and score >= threshold for label, score in rows)
    return {
        "positive_cases": positives,
        "negative_cases": negatives,
        "true_positive": tp,
        "false_positive": fp,
        "recall": tp / positives if positives else None,
        "false_positive_rate": fp / negatives if negatives else None,
    }


def calibrate(
    cases: list[CalibrationCase],
    observations: list[Observation],
    category: str,
    budget: Budget,
) -> dict[str, Any]:
    validation, held_out = partition_cases(cases)
    observed = {row.id: row.result for row in observations}
    if len(observed) != len(observations) or set(observed) != {
        case.id for case in validation
    }:
        raise ValueError(
            "provide each validation result exactly once and no test results"
        )
    if len({result.request_id for result in observed.values()}) != len(observed):
        raise ValueError("validation results must have distinct request IDs")
    provenance: ModelProvenance | None = None
    rows = []
    slices: dict[str, list[tuple[bool, float]]] = defaultdict(list)
    for case in validation:
        result = observed[case.id]
        if result.input_type != case.input_type or result.fallback:
            raise ValueError("result modality must match and fallback is not supported")
        signature = ModelProvenance.from_result(result)
        if provenance is None:
            provenance = signature
        elif provenance != signature:
            raise ValueError(
                "model or preprocessing provenance changed within validation"
            )
        findings = [
            finding for finding in result.findings if finding.category == category
        ]
        if len(findings) != 1 or findings[0].source != "model":
            raise ValueError(
                "every validation result must contain the model category score"
            )
        rows.append((case.expected == "block", findings[0].score))
        slices[case.slice].append(rows[-1])
    threshold, measured = select_cutoff(rows, budget)
    rule = (
        PolicyRule(category=category, review_threshold=threshold)
        if threshold is not None
        else None
    )
    return {
        "status": "proposal" if rule is not None else "no_candidate",
        "threshold": threshold,
        "policy_rule": rule.model_dump(mode="json", exclude_none=True)
        if rule
        else None,
        "category": category,
        "budget": budget.model_dump(),
        "provenance": provenance.model_dump(mode="json") if provenance else None,
        "validation_cases": len(validation),
        "validation_positive_cases": sum(label for label, _ in rows),
        "validation_negative_cases": sum(not label for label, _ in rows),
        "held_out_cases_reserved": len(held_out),
        "validation_groups": len({case.group for case in validation}),
        "held_out_groups": len({case.group for case in held_out}),
        "validation_metrics": measured,
        "validation_slices": (
            {
                name: slice_metrics(values, threshold)
                for name, values in sorted(slices.items())
            }
            if threshold is not None
            else None
        ),
        "limitations": (
            "Empirical validation rates, not production guarantees. Test scores and "
            "labels are not used for threshold selection. Caller-declared source "
            "groups do not establish independence or absence of training overlap. "
            "This proposes one model category review threshold, not a complete policy."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Propose a review threshold using validation scores only"
    )
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--validation-results", type=Path, required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--max-false-positive-rate", type=float, required=True)
    parser.add_argument("--min-recall", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        case_data = args.cases.read_bytes()
        result_data = args.validation_results.read_bytes()
        cases = [CalibrationCase.model_validate(row) for row in json.loads(case_data)]
        observations = [
            Observation.model_validate_json(line) for line in result_data.splitlines()
        ]
        category = PolicyRule(category=args.category, review_threshold=0.5).category
        budget = Budget(
            max_false_positive_rate=args.max_false_positive_rate,
            min_recall=args.min_recall,
        )
        result = calibrate(cases, observations, category, budget)
        result["inputs"] = {
            "cases_sha256": hashlib.sha256(case_data).hexdigest(),
            "validation_results_sha256": hashlib.sha256(result_data).hexdigest(),
        }
        result["calibrator_sha256"] = digest_file(Path(__file__))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as output:
            output.write(json.dumps(result, indent=2, allow_nan=False) + "\n")
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"Calibration failed: {exc}\n")
    if result["status"] != "proposal":
        parser.exit(2, "No cutoff met both empirical validation budgets.\n")


if __name__ == "__main__":
    main()
