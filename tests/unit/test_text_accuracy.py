import math

import pytest

from evaluation.text import (
    Row,
    auc,
    bands,
    cutoff_for,
    deduplicate,
    evaluate,
    fingerprint,
    split_of,
    wilson,
)


def test_auc_matches_hand_computed_values() -> None:
    assert auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert auc([0.1, 0.2], [0.9, 0.8]) == 0.0
    assert auc([0.5], [0.5]) == 0.5
    # one of the four toxic/clean pairs is misordered: 3 of 4 correct
    assert auc([0.8, 0.4], [0.5, 0.1]) == pytest.approx(0.75)


def test_auc_counts_tied_scores_as_half() -> None:
    assert auc([0.5, 0.9], [0.5, 0.1]) == pytest.approx(0.875)


def test_wilson_interval_is_sensible() -> None:
    assert wilson(0, 0) is None
    low, high = wilson(50, 100) or (0, 0)
    assert 0.40 < low < 0.41 and 0.59 < high < 0.60
    low, high = wilson(0, 20) or (1, 1)
    assert low == 0.0 and 0.1 < high < 0.2
    low, high = wilson(20, 20) or (0, 0)
    assert 0.8 < low < 0.9 and high == 1.0


def test_cutoff_respects_the_false_positive_budget() -> None:
    clean = [i / 100 for i in range(100)]  # 0.00 .. 0.99
    toxic = [0.97, 0.98, 0.99, 0.5]
    cutoff = cutoff_for(toxic, clean, 0.05)
    assert sum(s >= cutoff for s in clean) / len(clean) <= 0.05
    assert sum(s >= cutoff for s in clean) == 5  # as lenient as the budget allows
    assert cutoff_for(toxic, clean, 0.0) > max(clean)  # nothing may be flagged


def test_cutoff_when_the_budget_cannot_be_met_flags_nothing() -> None:
    assert cutoff_for([0.9], [0.9, 0.95], 0.0) > 0.95


def test_bands_partition_every_example() -> None:
    result = bands([0.9, 0.6, 0.2], [0.1, 0.55, 0.95], review=0.5, block=0.8)
    assert result["toxic"] == {"allow": 1, "review": 1, "block": 1}
    assert result["clean"] == {"allow": 1, "review": 1, "block": 1}


def test_normalisation_and_split_are_stable() -> None:
    assert fingerprint("Hello   WORLD") == fingerprint(" hello world ")
    assert split_of("same text") == split_of("  SAME   text ")
    assert {split_of(f"example {i}") for i in range(50)} == {"validation", "test"}


def test_deduplication_collapses_copies_and_drops_conflicts() -> None:
    kept, counts = deduplicate(
        [("a b", True), ("A  B", True), ("c", True), ("c", False), ("d", False)]
    )
    assert sorted(fingerprint(text) for text, _ in kept) == sorted(
        [fingerprint("a b"), fingerprint("d")]
    )
    assert counts == {"duplicates_collapsed": 1, "conflicting_rows_dropped": 2}


def test_evaluate_picks_cutoffs_on_validation_and_scores_the_held_out_half() -> None:
    rows: list[Row] = []
    scores: list[float | None] = []
    for i in range(200):
        for positive, score in ((True, 0.6 + (i % 40) / 100), (False, (i % 50) / 100)):
            rows.append(
                Row(f"{positive} {i}", positive, "validation" if i % 2 else "test")
            )
            scores.append(score)
    scores[0] = None  # a failed request is counted, never scored
    report = evaluate(rows, scores)
    assert report["failed_requests"] == 1
    assert report["gate"]["met_on_validation"] is True
    chosen = report["test_at_chosen_cutoff"]
    assert chosen["false_positive_rate"]["value"] is not None
    assert not math.isnan(report["auc_test"]["value"])
    assert report["test_bands"]["toxic"]["allow"] >= 0
