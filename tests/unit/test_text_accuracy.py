import json
import math
from pathlib import Path

import pytest

from evaluation.text import (
    Row,
    any_of,
    auc,
    bands,
    brier,
    cutoff_for,
    deduplicate,
    ece,
    evaluate,
    fingerprint,
    isotonic,
    prevalence_table,
    reliability,
    report_command,
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


def test_brier_is_the_mean_squared_error_of_the_score() -> None:
    assert brier([1.0, 0.0], [1, 0]) == 0.0
    assert brier([0.5, 0.5], [1, 0]) == 0.25
    assert brier([0.0, 1.0], [1, 0]) == 1.0


def test_reliability_bins_include_a_score_of_exactly_one() -> None:
    table = reliability([0.05, 0.05, 0.95, 1.0], [0, 0, 1, 1])
    assert [(item["n"], item["observed"]) for item in table] == [(2, 0.0), (2, 1.0)]
    assert table[-1]["range"] == [0.9, 1.0]


def test_ece_is_zero_when_scores_match_the_observed_rate() -> None:
    assert ece([0.7] * 10, [1] * 7 + [0] * 3) == pytest.approx(0.0)
    assert ece([0.9] * 10, [1] * 5 + [0] * 5) == pytest.approx(0.4)


def test_isotonic_pools_violations_and_stays_monotone() -> None:
    fitted = isotonic([0.1, 0.2, 0.3, 0.4], [0, 1, 0, 1])
    assert [fitted(x) for x in (0.05, 0.1, 0.25, 0.3, 0.35, 0.9)] == [
        0.0,
        0.0,
        0.5,
        0.5,
        1.0,
        1.0,
    ]
    spread = [i / 50 for i in range(50)]
    labels = [(i * 7919) % 2 for i in range(50)]
    fit = isotonic(spread, labels)
    mapped = [fit(x) for x in spread]
    assert mapped == sorted(mapped)
    assert sum(mapped) / 50 == pytest.approx(sum(labels) / 50)


def test_prevalence_table_shows_why_rare_toxicity_swamps_reviewers() -> None:
    one_percent = prevalence_table(recall=0.5, false_positive_rate=0.05)[0]
    assert one_percent["prevalence"] == 0.01
    assert one_percent["flagged_per_1000"] == 54.5
    assert one_percent["toxic_among_flagged"] == pytest.approx(0.0917, abs=1e-4)
    assert one_percent["toxic_missed_per_1000"] == 5.0
    assert prevalence_table(0.0, 0.0)[0]["toxic_among_flagged"] is None


def synthetic(
    rows_per_split: int = 100,
) -> tuple[list[Row], dict[str, list[float | None]]]:
    rows: list[Row] = []
    a: list[float | None] = []
    b: list[float | None] = []
    for split in ("validation", "test"):
        for i in range(rows_per_split):
            clean = i / rows_per_split  # 0.00 .. 0.99
            rows += [Row("", False, split), Row("", True, split)]
            a += [clean * 0.6, 0.55 + clean * 0.4 if i % 2 == 0 else clean * 0.6]
            b += [clean * 0.6, 0.55 + clean * 0.4 if i % 2 == 1 else clean * 0.6]
    return rows, {"A": a, "B": b}


def test_either_of_two_models_catches_what_each_misses_alone() -> None:
    rows, scores = synthetic()
    results = {name: evaluate(rows, values) for name, values in scores.items()}
    combined = any_of(rows, scores, results)
    assert sorted(combined["models"]) == ["A", "B"]
    each = [r["test_at_chosen_cutoff"]["recall"]["value"] for r in results.values()]
    assert combined["test"]["recall"]["value"] > max(each)
    assert combined["each_model_false_positive_budget"] == 0.025


def test_report_renders_end_to_end_from_saved_scores(tmp_path: Path) -> None:
    rows, scores = synthetic()
    cohort = {
        "source_rows": len(rows),
        "duplicates_collapsed": 0,
        "conflicting_rows_dropped": 0,
        "too_long_excluded": 0,
    }
    saved = {
        "sources": {
            "ToxiGen (English)": {
                "cohort": cohort,
                "rows": [{"split": r.split, "positive": r.positive} for r in rows],
                "scores": {
                    "Horizon Small": scores["A"],
                    "Horizon Base": scores["B"],
                },
            }
        }
    }
    path = tmp_path / "scores.json"
    path.write_text(json.dumps(saved))
    report_command(path, tmp_path / "report.json", tmp_path / "report.md")
    page = (tmp_path / "report.md").read_text()
    for heading in (
        "## Result",
        "### Review and block bands",
        "### Do the scores mean what they say?",
        "### What this means when toxic text is rare",
        "### Does a second model help?",
        "## Limits",
    ):
        assert heading in page
    parsed = json.loads((tmp_path / "report.json").read_text())
    assert (
        "calibration"
        in parsed["sources"]["ToxiGen (English)"]["models"]["Horizon Small"]
    )
