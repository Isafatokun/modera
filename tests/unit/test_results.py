import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from modera.results import ModerationResult


@pytest.fixture
def payload() -> dict[str, Any]:
    return {
        "request_id": "req_0123456789abcdef0123456789abcdef",
        "input_type": "image",
        "policy": {"id": "default", "version": "1"},
        "decision": {"action": "review", "reasons": ["violence"]},
        "findings": [
            {"category": "violence", "score": 0.62},
            {"category": "sexual", "score": 0.01},
        ],
        "coverage": {
            "evaluated": ["violence", "sexual"],
            "unsupported": ["hate"],
            "not_applicable": [],
        },
        "models": [{"id": "image-classifier", "revision": "example-1"}],
        "preprocessing_version": "image-v1",
    }


@pytest.mark.parametrize("input_type", ["text", "image"])
@pytest.mark.parametrize("action", ["allow", "review", "block"])
def test_result_round_trip(
    payload: dict[str, Any], input_type: str, action: str
) -> None:
    payload["input_type"] = input_type
    payload["decision"] = {
        "action": action,
        "reasons": [] if action == "allow" else ["violence"],
    }
    result = ModerationResult.model_validate(payload)
    assert ModerationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "score", [-0.01, 1.01, float("nan"), float("inf"), "0.5", True]
)
def test_invalid_scores_are_rejected(payload: dict[str, Any], score: object) -> None:
    payload["findings"][0]["score"] = score
    with pytest.raises(ValidationError):
        ModerationResult.model_validate(payload)


@pytest.mark.parametrize("score", [0.0, 1.0])
def test_score_boundaries(payload: dict[str, Any], score: float) -> None:
    payload["findings"][0]["score"] = score
    assert ModerationResult.model_validate(payload).findings[0].score == score


@pytest.mark.parametrize(
    "decision",
    [
        {"action": "allow", "reasons": ["violence"]},
        {"action": "review", "reasons": []},
        {"action": "block", "reasons": []},
        {"action": "review", "reasons": ["violence", "violence"]},
        {"action": "pending", "reasons": []},
    ],
)
def test_inconsistent_decisions_are_rejected(
    payload: dict[str, Any], decision: object
) -> None:
    payload["decision"] = decision
    with pytest.raises(ValidationError):
        ModerationResult.model_validate(payload)


@pytest.mark.parametrize("field", ["evaluated", "unsupported", "not_applicable"])
def test_conflicting_coverage_is_rejected(payload: dict[str, Any], field: str) -> None:
    payload["coverage"][field].append("violence")
    with pytest.raises(ValidationError):
        ModerationResult.model_validate(payload)


@pytest.mark.parametrize("category", ["sexual", "hate"])
def test_findings_must_match_evaluated_checks(
    payload: dict[str, Any], category: str
) -> None:
    payload["findings"].append({"category": category, "score": 0.0})
    with pytest.raises(ValidationError):
        ModerationResult.model_validate(payload)


def test_evaluated_check_requires_a_finding(payload: dict[str, Any]) -> None:
    payload["coverage"]["evaluated"].append("self_harm")
    with pytest.raises(ValidationError):
        ModerationResult.model_validate(payload)


def test_empty_evaluation_cannot_produce_allow(payload: dict[str, Any]) -> None:
    payload["decision"] = {"action": "allow", "reasons": []}
    payload["findings"] = []
    payload["coverage"]["evaluated"] = []
    with pytest.raises(ValidationError):
        ModerationResult.model_validate(payload)


@pytest.mark.parametrize(
    "field", ["request_id", "policy", "models", "preprocessing_version"]
)
def test_provenance_is_required(payload: dict[str, Any], field: str) -> None:
    payload.pop(field)
    with pytest.raises(ValidationError):
        ModerationResult.model_validate(payload)


@pytest.mark.parametrize("version", ["", " ", "revision with spaces", 1])
def test_policy_version_is_a_nonempty_string(
    payload: dict[str, Any], version: object
) -> None:
    payload["policy"]["version"] = version
    with pytest.raises(ValidationError):
        ModerationResult.model_validate(payload)


def test_model_identifiers_cannot_repeat(payload: dict[str, Any]) -> None:
    payload["models"].append({"id": "image-classifier", "revision": "example-2"})
    with pytest.raises(ValidationError):
        ModerationResult.model_validate(payload)


def test_unknown_fields_are_rejected(payload: dict[str, Any]) -> None:
    payload["findings"][0]["flagged"] = True
    with pytest.raises(ValidationError):
        ModerationResult.model_validate(payload)


def test_categories_and_policy_reasons_are_extensible(payload: dict[str, Any]) -> None:
    payload["findings"].append({"category": "new_category", "score": 0.2})
    payload["coverage"]["evaluated"].append("new_category")
    payload["decision"]["reasons"] = ["custom_policy_rule"]
    result = ModerationResult.model_validate(payload)
    assert result.decision.reasons == ["custom_policy_rule"]


def test_documented_result_matches_the_schema() -> None:
    reference = Path(__file__).resolve().parents[2] / "docs" / "api.md"
    section = reference.read_text().split("## Moderation result", 1)[1]
    example = section.split("```json\n", 1)[1].split("```", 1)[0]
    result = ModerationResult.model_validate(json.loads(example))
    assert result.decision.action == "review"
