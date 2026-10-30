import json

import pytest
from pydantic import ValidationError

from modera.errors import APIError
from modera.policies import Policy, evaluate_policy
from modera.results import Coverage, Finding


def policy(operator: str = "all_of", /, **overrides: object) -> Policy:
    composite = {
        "id": "pair",
        "action": "review",
        operator: [
            {"category": "detect.a", "min": 0.7},
            {"category": "classify.b", "min": 0.6},
        ],
        **overrides,
    }
    return Policy.model_validate_json(
        json.dumps(
            {
                "id": "default",
                "version": "2",
                "image_composites": [composite],
            }
        )
    )


@pytest.mark.parametrize(
    "operator,scores,action",
    [
        ("all_of", (0.7, 0.6), "review"),
        ("all_of", (0.69, 0.9), "allow"),
        ("all_of", (0.9, 0.59), "allow"),
        ("any_of", (0.69, 0.6), "review"),
        ("any_of", (0.7, 0.59), "review"),
        ("any_of", (0.69, 0.59), "allow"),
    ],
)
def test_composites_are_boolean_and_keep_model_scores(
    operator: str,
    scores: tuple[float, float],
    action: str,
) -> None:
    findings = [
        Finding(category=c, score=s)
        for c, s in zip(("detect.a", "classify.b"), scores, strict=True)
    ]
    coverage = Coverage(
        evaluated=[f.category for f in findings], unsupported=[], not_applicable=[]
    )
    before = [f.model_dump() for f in findings]
    decision = evaluate_policy(policy(operator), "image", findings, coverage)
    assert decision.action == action
    assert decision.matched_rules == (["pair"] if action == "review" else [])
    assert [f.model_dump() for f in findings] == before
    if operator == "any_of" and action == "review":
        assert decision.reasons == [
            c
            for c, score, minimum in zip(
                ("detect.a", "classify.b"), scores, (0.7, 0.6), strict=True
            )
            if score >= minimum
        ]


@pytest.mark.parametrize("unsupported,status", [(True, 400), (False, 503)])
def test_even_any_requires_complete_evidence(unsupported: bool, status: int) -> None:
    with pytest.raises(APIError) as failure:
        evaluate_policy(
            policy("any_of"),
            "image",
            [Finding(category="detect.a", score=1.0)],
            Coverage(
                evaluated=["detect.a"],
                unsupported=["classify.b"] if unsupported else [],
                not_applicable=[],
            ),
        )
    assert failure.value.status_code == status


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope": "same_subject"},
        {"any_of": [{"category": "detect.a", "min": 0.7}]},
        {"all_of": []},
        {"all_of": [{"category": "detect.a", "min": 0.7}] * 2},
        {"all_of": [{"category": "detect.a", "min": 1.1}]},
    ],
)
def test_ambiguous_or_unsupported_composites_fail(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        policy(**overrides)


def test_block_rule_wins_over_review_composite() -> None:
    selection = policy().model_copy(update={"image": ()})
    raw = json.loads(selection.model_dump_json())
    raw["image"] = [{"category": "detect.a", "block_threshold": 0.8}]
    selection = Policy.model_validate_json(json.dumps(raw))
    decision = evaluate_policy(
        selection,
        "image",
        [
            Finding(category="detect.a", score=0.9),
            Finding(category="classify.b", score=0.9),
        ],
        Coverage(
            evaluated=["detect.a", "classify.b"], unsupported=[], not_applicable=[]
        ),
    )
    assert decision.action == "block"
    assert decision.reasons == ["detect.a"]
    assert decision.matched_rules == []
