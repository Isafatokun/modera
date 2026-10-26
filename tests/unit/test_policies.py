import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from modera.errors import APIError
from modera.policies import (
    Policy,
    PolicyConfig,
    PolicyRule,
    evaluate_policy,
    load_policies,
)
from modera.results import Coverage, Finding


@pytest.fixture
def policy() -> Policy:
    return Policy.model_validate_json(
        json.dumps(
            {
                "id": "default",
                "version": "1",
                "text": [
                    {
                        "category": "violence",
                        "review_threshold": 0.5,
                        "block_threshold": 0.85,
                    }
                ],
                "image": [
                    {
                        "category": "sexual",
                        "review_threshold": 0.5,
                        "block_threshold": 0.85,
                    },
                    {
                        "category": "violence",
                        "review_threshold": 0.5,
                        "block_threshold": 0.85,
                    },
                ],
            }
        )
    )


@pytest.mark.parametrize(
    "score,action",
    [
        (0.0, "allow"),
        (0.49, "allow"),
        (0.5, "review"),
        (0.84, "review"),
        (0.85, "block"),
        (1.0, "block"),
    ],
)
def test_threshold_boundaries(policy: Policy, score: float, action: str) -> None:
    decision = evaluate_policy(
        policy,
        "text",
        [Finding(category="violence", score=score)],
        Coverage(evaluated=["violence"], unsupported=[], not_applicable=[]),
    )
    assert decision.action == action
    assert decision.reasons == ([] if action == "allow" else ["violence"])


def test_block_precedes_review(policy: Policy) -> None:
    decision = evaluate_policy(
        policy,
        "image",
        [
            Finding(category="sexual", score=0.6),
            Finding(category="violence", score=0.9),
        ],
        Coverage(evaluated=["sexual", "violence"], unsupported=[], not_applicable=[]),
    )
    assert decision.action == "block"
    assert decision.reasons == ["violence"]


@pytest.mark.parametrize(
    "threshold,action", [("review_threshold", "review"), ("block_threshold", "block")]
)
def test_single_threshold_rules(threshold: str, action: str) -> None:
    policy = Policy.model_validate_json(
        json.dumps(
            {
                "id": "default",
                "version": "1",
                "text": [{"category": "violence", threshold: 0.8}],
            }
        )
    )
    coverage = Coverage(evaluated=["violence"], unsupported=[], not_applicable=[])
    assert (
        evaluate_policy(
            policy, "text", [Finding(category="violence", score=0.8)], coverage
        ).action
        == action
    )
    assert (
        evaluate_policy(
            policy, "text", [Finding(category="violence", score=0.79)], coverage
        ).action
        == "allow"
    )


def test_policy_without_rules_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Policy.model_validate_json('{"id":"default","version":"1"}')


def test_reason_order_follows_policy_order(policy: Policy) -> None:
    decision = evaluate_policy(
        policy,
        "image",
        [
            Finding(category="violence", score=0.95),
            Finding(category="sexual", score=0.9),
        ],
        Coverage(evaluated=["violence", "sexual"], unsupported=[], not_applicable=[]),
    )
    assert decision.reasons == ["sexual", "violence"]


@pytest.mark.parametrize("missing", ["unsupported", "not_applicable", "unavailable"])
@pytest.mark.parametrize("score", [0.1, 1.0])
def test_missing_required_checks_never_allow(
    policy: Policy, missing: str, score: float
) -> None:
    coverage = Coverage(
        evaluated=["violence"],
        unsupported=["sexual"] if missing == "unsupported" else [],
        not_applicable=["sexual"] if missing == "not_applicable" else [],
    )
    with pytest.raises(APIError) as failure:
        evaluate_policy(
            policy, "image", [Finding(category="violence", score=score)], coverage
        )
    error = failure.value
    assert error.code == (
        "check_unavailable" if missing == "unavailable" else "policy_unsupported"
    )
    assert error.retryable is (missing == "unavailable")
    assert error.status_code == (503 if missing == "unavailable" else 400)


def test_optional_unsupported_rule_can_be_skipped(policy: Policy) -> None:
    payload = policy.model_dump(mode="json")
    payload["image"][0]["required"] = False
    optional = Policy.model_validate_json(json.dumps(payload))
    decision = evaluate_policy(
        optional,
        "image",
        [Finding(category="violence", score=0.1)],
        Coverage(evaluated=["violence"], unsupported=["sexual"], not_applicable=[]),
    )
    assert decision.action == "allow"


def test_optional_rule_failure_is_not_treated_as_unsupported(policy: Policy) -> None:
    payload = policy.model_dump(mode="json")
    payload["image"][0]["required"] = False
    optional = Policy.model_validate_json(json.dumps(payload))
    with pytest.raises(APIError) as failure:
        evaluate_policy(
            optional,
            "image",
            [Finding(category="violence", score=0.1)],
            Coverage(evaluated=["violence"], unsupported=[], not_applicable=[]),
        )
    assert failure.value.code == "check_unavailable"
    assert failure.value.headers == {"Retry-After": "2"}


def test_unrelated_findings_cannot_produce_allow() -> None:
    policy = Policy.model_validate_json(
        '{"id":"default","version":"1","text":'
        '[{"category":"violence","required":false,"block_threshold":0.8}]}'
    )
    with pytest.raises(APIError) as failure:
        evaluate_policy(
            policy,
            "text",
            [Finding(category="sexual", score=0.1)],
            Coverage(evaluated=["sexual"], unsupported=["violence"], not_applicable=[]),
        )
    assert failure.value.code == "policy_not_applicable"


@pytest.mark.parametrize(
    "findings", [[], [Finding(category="violence", score=0.1)] * 2]
)
def test_inconsistent_findings_are_rejected(
    policy: Policy, findings: list[Finding]
) -> None:
    with pytest.raises(ValueError, match="findings must match"):
        evaluate_policy(
            policy,
            "text",
            findings,
            Coverage(evaluated=["violence"], unsupported=[], not_applicable=[]),
        )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"block_threshold": -0.1},
        {"block_threshold": 1.1},
        {"block_threshold": True},
        {"block_threshold": "0.8"},
        {"block_threshold": float("nan")},
        {"block_threshold": float("inf")},
        {"review_threshold": 0.8, "block_threshold": 0.8},
        {"review_threshold": 0.9, "block_threshold": 0.8},
        {"block_threshold": 0.8, "required": "false"},
        {"block_threshold": 0.8, "extra": True},
    ],
)
def test_invalid_rules_are_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PolicyRule.model_validate({"category": "violence", **fields})


def test_policies_are_immutable(policy: Policy) -> None:
    with pytest.raises(ValidationError):
        policy.text[0].block_threshold = 0.1  # type: ignore[misc]  # Verify runtime immutability.


def test_duplicate_categories_and_ids_are_rejected(policy: Policy) -> None:
    payload = policy.model_dump(mode="json")
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate_json(json.dumps({"policies": [payload, payload]}))
    payload["text"].append(payload["text"][0])
    with pytest.raises(ValidationError):
        Policy.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("content", [b"{", b'{"policies":[]}', b'{"secret":"value"}'])
def test_invalid_configuration_fails_without_echoing_content(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "policies.json"
    path.write_bytes(content)
    with pytest.raises(ValueError) as failure:
        load_policies(path)
    assert "secret" not in str(failure.value)
    assert "value" not in str(failure.value)


def test_missing_or_oversized_config_fails(tmp_path: Path) -> None:
    path = tmp_path / "policies.json"
    with pytest.raises(ValueError):
        load_policies(path)
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError):
        load_policies(path)


def test_example_loads_and_no_file_does_not_install_defaults() -> None:
    path = Path(__file__).resolve().parents[2] / "examples" / "policies.json"
    assert load_policies(path)["default"].version == "1"
    assert not load_policies(None)
