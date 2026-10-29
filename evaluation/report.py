from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Action = Literal["allow", "review", "block"]
Name = Annotated[str, Field(min_length=1, max_length=200)]
ACTIONS = ("allow", "review", "block")


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Name
    group: Name
    split: Literal["validation", "test", "diagnostic"]
    input_type: Literal["text", "image"]
    slice: Name
    expected: Action


def summarize(rows: list[tuple[Case, Action | None]]) -> dict[str, Any]:
    matrix = {expected: dict.fromkeys(ACTIONS, 0) for expected in ACTIONS}
    failures: Counter[str] = Counter()
    for case, predicted in rows:
        if predicted is None:
            failures[case.expected] += 1
        else:
            matrix[case.expected][predicted] += 1
    completed = sum(sum(row.values()) for row in matrix.values())
    correct = sum(matrix[action][action] for action in ACTIONS)
    metrics = {}
    for action in ACTIONS:
        tp = matrix[action][action]
        predicted_count = sum(matrix[label][action] for label in ACTIONS)
        expected_count = sum(matrix[action].values())
        all_cases = expected_count + failures[action]
        metrics[action] = {
            "precision": tp / predicted_count if predicted_count else None,
            "recall_completed": tp / expected_count if expected_count else None,
            "recall_all_cases": tp / all_cases if all_cases else None,
            "expected_completed": expected_count,
            "expected_failed": failures[action],
        }
    return {
        "cases": len(rows),
        "source_groups": len({case.group for case, _ in rows}),
        "completed": completed,
        "failed": len(rows) - completed,
        "completion_rate": completed / len(rows) if rows else None,
        "accuracy_completed": correct / completed if completed else None,
        "confusion": matrix,
        "actions": metrics,
    }
