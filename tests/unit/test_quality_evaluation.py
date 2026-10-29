import pytest

from evaluation.compare_text import fingerprint, select_test
from evaluation.report import Case, summarize


def case(identifier: str) -> Case:
    return Case(
        id=identifier,
        group=identifier,
        split="test",
        input_type="text",
        slice="en",
        expected="block",
    )


def test_failures_reduce_end_to_end_recall_without_becoming_allow() -> None:
    result = summarize([(case("a"), "block"), (case("b"), None)])
    assert result["actions"]["block"]["recall_completed"] == 1
    assert result["actions"]["block"]["recall_all_cases"] == 0.5
    assert result["completion_rate"] == 0.5
    assert result["confusion"]["block"]["allow"] == 0


def test_missing_classes_do_not_get_perfect_scores() -> None:
    result = summarize([])
    assert result["actions"]["block"]["precision"] is None
    assert result["accuracy_completed"] is None


def test_normalization_matches_unicode_and_whitespace_variants() -> None:
    assert fingerprint("  Café\nWORLD ") == fingerprint("cafe\u0301 world")


def test_sampling_excludes_training_text_and_conflicting_groups() -> None:
    train = [{"text": "training text"}]
    test = [
        {"text": "TRAINING TEXT", "toxic": 0, "language": "en"},
        {"text": "ambiguous", "toxic": 0, "language": "en"},
        {"text": "AMBIGUOUS", "toxic": 1, "language": "en"},
        {"text": "clear positive", "toxic": 1, "language": "en"},
        {"text": "clear negative", "toxic": 0, "language": "en"},
        {"text": "clear negative", "toxic": 0, "language": "en"},
    ]
    selected, excluded = select_test(train, test, 1)
    assert {row["text"] for row in selected} == {"clear positive", "clear negative"}
    assert excluded["training_overlap_groups_excluded"] == 1
    assert excluded["conflicting_label_groups_excluded"] == 1
    assert excluded["duplicate_test_rows_collapsed"] == 1
    assert (selected, excluded) == select_test(train, list(reversed(test)), 1)


def test_training_exclusion_applies_across_languages() -> None:
    rows = [{"text": "same text", "toxic": 0, "language": "fr"}]
    with pytest.raises(ValueError, match="not enough"):
        select_test([{"text": "SAME TEXT"}], rows, 1)


@pytest.mark.parametrize("label", [True, 2, "1"])
def test_source_labels_require_binary_integers(label: object) -> None:
    with pytest.raises(ValueError, match="invalid source"):
        select_test([], [{"text": "example", "toxic": label, "language": "en"}], 1)


def test_sampling_rejects_invalid_sample_size() -> None:
    with pytest.raises(ValueError):
        select_test([], [], 0)


def test_sampling_rejects_empty_source_instead_of_claiming_completion() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        select_test([], [], 1)
