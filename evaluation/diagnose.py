import argparse
import json
import math
from collections import Counter, defaultdict
from itertools import groupby
from pathlib import Path
from typing import Any, Self, TypedDict

from pydantic import BaseModel, ConfigDict, model_validator

from evaluation.compare_text import digest_file
from evaluation.report import Action, Case, Name
from modera.results import Score


class Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: Name
    scores: dict[str, Score] | None
    action: Action | None
    error: Name | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.error is None:
            if (
                self.scores is None
                or set(self.scores) != {"toxicity"}
                or self.action is None
            ):
                raise ValueError(
                    "successful prediction requires toxicity and an action"
                )
        elif self.action is not None:
            raise ValueError("failed prediction must not contain a decision")
        return self


class LanguageRanking(TypedDict):
    roc_auc_completed: float | None
    toxic_completed: int
    non_toxic_completed: int
    toxic_failed: int
    non_toxic_failed: int
    errors: dict[str, int]


def roc_auc(rows: list[tuple[bool, float]]) -> float | None:
    if any(not math.isfinite(score) or not 0 <= score <= 1 for _, score in rows):
        raise ValueError("scores must be finite and between zero and one")
    positives = sum(label for label, _ in rows)
    negatives = len(rows) - positives
    if not positives or not negatives:
        return None
    twice_wins = 0
    negatives_seen = 0
    ordered = sorted(rows, key=lambda row: row[1])
    for _, tied in groupby(ordered, key=lambda row: row[1]):
        labels = [label for label, _ in tied]
        positive_count = sum(labels)
        negative_count = len(labels) - positive_count
        twice_wins += positive_count * (2 * negatives_seen + negative_count)
        negatives_seen += negative_count
    return twice_wins / (2 * positives * negatives)


def diagnose(cases: list[Case], predictions: list[Prediction]) -> dict[str, Any]:
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("cases must be nonempty and unique")
    observed = {prediction.id: prediction for prediction in predictions}
    if len(observed) != len(predictions) or set(observed) != {
        case.id for case in cases
    }:
        raise ValueError("predictions must match all case IDs exactly once")
    ranked: dict[str, list[tuple[bool, float]]] = defaultdict(list)
    failures: dict[str, Counter[str]] = defaultdict(Counter)
    errors: dict[str, Counter[str]] = defaultdict(Counter)
    languages = sorted({case.slice for case in cases})
    for case in cases:
        if case.input_type != "text" or case.expected == "review":
            raise ValueError("ranking requires binary labelled text cases")
        prediction = observed[case.id]
        if prediction.error is not None:
            failures[case.slice][case.expected] += 1
            errors[case.slice][prediction.error] += 1
            continue
        assert prediction.scores is not None
        ranked[case.slice].append(
            (case.expected == "block", prediction.scores["toxicity"])
        )
    results: dict[str, LanguageRanking] = {}
    for language in languages:
        rows = ranked[language]
        positive_count = sum(label for label, _ in rows)
        results[language] = {
            "roc_auc_completed": roc_auc(rows),
            "toxic_completed": positive_count,
            "non_toxic_completed": len(rows) - positive_count,
            "toxic_failed": failures[language]["block"],
            "non_toxic_failed": failures[language]["allow"],
            "errors": dict(errors[language]),
        }
    aucs = [metrics["roc_auc_completed"] for metrics in results.values()]
    defined = [value for value in aucs if value is not None]
    return {
        "scope": "Ranking diagnostic on existing scores; no cutoff is selected.",
        "macro_language_roc_auc_completed": (
            sum(defined) / len(defined) if len(defined) == len(languages) else None
        ),
        "undefined_languages": [
            language
            for language, metrics in results.items()
            if metrics["roc_auc_completed"] is None
        ],
        "languages": results,
        "limitations": (
            "AUC uses completed cases only; failures are reported separately. "
            "Macro AUC is undefined if any language lacks either class. "
            "This is not a new validation set, a production threshold recommendation "
            "or evidence that scores are calibrated probabilities."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnose ranking without selecting test-set thresholds"
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    protocol = json.loads((args.directory / "protocol.json").read_bytes())
    report = json.loads((args.run / "report.json").read_bytes())
    if digest_file(args.directory / "cases.json") != protocol["cases_sha256"]:
        raise ValueError("frozen sample changed")
    if report["protocol"] != protocol:
        raise ValueError("run and dataset protocols differ")
    if digest_file(args.run / "predictions.jsonl") != report["predictions_sha256"]:
        raise ValueError("saved predictions changed")
    cases = [
        Case.model_validate(row["case"])
        for row in json.loads((args.directory / "cases.json").read_bytes())
    ]
    predictions = [
        Prediction.model_validate_json(line)
        for line in (args.run / "predictions.jsonl").read_bytes().splitlines()
    ]
    result = diagnose(cases, predictions)
    result["candidate"] = report["candidate"]
    result["cases_sha256"] = protocol["cases_sha256"]
    result["predictions_sha256"] = report["predictions_sha256"]
    result["analyzer_sha256"] = digest_file(Path(__file__))
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
