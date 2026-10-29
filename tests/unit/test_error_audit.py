from typing import Any

import pytest

from evaluation.error_audit import select_errors
from modera.results import ModerationResult


def response(identifier: int, action: str) -> ModerationResult:
    return ModerationResult.model_validate(
        {
            "request_id": f"req_{identifier:032x}",
            "input_type": "text",
            "policy": {"id": "fixture", "version": "1"},
            "decision": {
                "action": action,
                "reasons": [] if action == "allow" else ["toxicity"],
            },
            "findings": [
                {"category": "toxicity", "score": 0.1 if action == "allow" else 0.9}
            ],
            "coverage": {
                "evaluated": ["toxicity"],
                "unsupported": [],
                "not_applicable": [],
            },
            "models": [{"id": "fixture/text", "revision": "a" * 40}],
            "preprocessing_version": "1",
        }
    )


def test_sampling_is_order_independent_and_keeps_error_strata_separate() -> None:
    cases: list[dict[str, Any]] = [
        {"id": str(n), "source": "fr", "positive": n < 10} for n in range(20)
    ]
    results = {str(n): response(n, "allow" if n < 10 else "review") for n in range(20)}
    first = select_errors(cases, results, 3)
    assert first == select_errors(list(reversed(cases)), results, 3)
    assert set(first) == {"fr/false_negative", "fr/false_positive"}
    assert all(len(rows) == 3 for rows in first.values())
    with pytest.raises(ValueError, match="exactly"):
        select_errors(cases, {k: v for k, v in results.items() if k != "0"}, 3)
    with pytest.raises(ValueError, match="unique"):
        select_errors([*cases, cases[0]], results, 3)
