import json
import re
from pathlib import Path

import pytest

from evaluation.cutoffs import (
    CutoffError,
    choose,
    load_sample,
    parse_label,
    run,
)
from evaluation.text import Row
from modera.model_config import TextModelSettings
from modera.policies import load_policies


def sample(
    clean_scores: list[float], toxic_scores: list[float]
) -> tuple[list[Row], list[float | None]]:
    from evaluation.text import split_of

    rows: list[Row] = []
    scores: list[float | None] = []
    for i, score in enumerate(clean_scores):
        text = f"clean {i}"
        rows.append(Row(text, False, split_of(text)))
        scores.append(score)
    for i, score in enumerate(toxic_scores):
        text = f"toxic {i}"
        rows.append(Row(text, True, split_of(text)))
        scores.append(score)
    return rows, scores


def separable(n: int = 400) -> tuple[list[Row], list[float | None]]:
    clean = [(i % 70) / 100 for i in range(n)]
    toxic = [0.5 + (i % 50) / 100 for i in range(n)]
    return sample(clean, toxic)


def test_cutoffs_hold_false_flags_to_the_budget_on_the_half_they_were_chosen_on() -> (
    None
):
    rows, scores = separable()
    result = choose(rows, scores, "toxicity", "default", "a" * 64)
    assert result["validation_at_review_cutoff"]["false_positive_rate"]["value"] <= 0.05
    rule = result["policy"]["policies"][0]["text"][0]
    assert rule["category"] == "toxicity"
    assert rule["review_threshold"] < rule["block_threshold"] <= 1.0
    held = result["test_at_review_cutoff"]
    assert held["recall"]["of"] > 50 and held["false_positive_rate"]["of"] > 50


def test_the_written_policy_loads_through_modera_itself(tmp_path: Path) -> None:
    rows, scores = separable()
    policy = choose(rows, scores, "toxicity", "default", "b" * 64)["policy"]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    loaded = load_policies(path)["default"]
    assert loaded.version == "calibrated-bbbbbbbb"
    assert (
        loaded.text[0].review_threshold
        == policy["policies"][0]["text"][0]["review_threshold"]
    )


def test_target_is_met_only_when_validation_and_held_out_agree() -> None:
    rows, scores = separable()
    assert choose(rows, scores, "toxicity", "default", "a" * 64)["target_met"] is False
    easy_rows, easy_scores = sample([0.01] * 200, [0.99] * 200)
    assert (
        choose(easy_rows, easy_scores, "toxicity", "default", "a" * 64)["target_met"]
        is True
    )


def test_block_cutoff_is_left_out_when_it_cannot_be_told_from_review() -> None:
    rows, scores = sample([0.1] * 200, [0.9] * 200)
    rule = choose(rows, scores, "toxicity", "default", "a" * 64)["policy"]["policies"][
        0
    ]["text"][0]
    assert "block_threshold" not in rule and rule["review_threshold"] == 0.9


def test_a_sample_too_small_to_measure_the_budget_is_refused() -> None:
    rows, scores = sample([0.1] * 30, [0.9] * 30)
    with pytest.raises(CutoffError, match="at least 50"):
        choose(rows, scores, "toxicity", "default", "a" * 64)


def test_an_impossible_budget_fails_instead_of_writing_a_bad_policy() -> None:
    rows, scores = sample([0.99] * 200, [0.5] * 200)
    with pytest.raises(CutoffError, match="no useful cutoff"):
        choose(rows, scores, "toxicity", "default", "a" * 64)


def test_failed_requests_are_counted_and_not_scored() -> None:
    rows, scores = separable()
    scores[0] = None
    assert choose(rows, scores, "toxicity", "default", "a" * 64)["failed_requests"] == 1


def test_labels_are_parsed_strictly() -> None:
    assert parse_label("TRUE") is True and parse_label("0") is False
    assert parse_label(True) is True
    with pytest.raises(CutoffError, match="unrecognised"):
        parse_label("maybe")


def test_samples_load_from_jsonl_and_csv_and_drop_conflicts(tmp_path: Path) -> None:
    jsonl = tmp_path / "s.jsonl"
    jsonl.write_text(
        '{"text": "a", "toxic": true}\n{"text": "A", "toxic": true}\n'
        '{"text": "b", "toxic": true}\n{"text": "b", "toxic": false}\n'
        '{"text": "c", "toxic": false}\n{"text": "  ", "toxic": true}\n'
    )
    rows, counts, _ = load_sample(jsonl)
    assert sorted(r.text.lower() for r in rows) == ["a", "c"]
    assert (
        counts["duplicates_collapsed"] == 1 and counts["conflicting_rows_dropped"] == 2
    )
    csv_file = tmp_path / "s.csv"
    csv_file.write_text("text,toxic\nhello,false\nbad,true\n")
    assert len(load_sample(csv_file)[0]) == 2
    with pytest.raises(CutoffError, match="needs a `text`"):
        (tmp_path / "bad.jsonl").write_text('{"text": "x"}\n')
        load_sample(tmp_path / "bad.jsonl")
    with pytest.raises(CutoffError, match=re.escape(".jsonl or .csv")):
        (tmp_path / "s.txt").write_text("x")
        load_sample(tmp_path / "s.txt")


def test_run_writes_a_valid_policy_and_report_and_never_overwrites(
    tmp_path: Path,
) -> None:
    rows, scores = separable()
    by_text = {row.text: score for row, score in zip(rows, scores, strict=True)}
    data = tmp_path / "sample.jsonl"
    data.write_text(
        "\n".join(json.dumps({"text": r.text, "toxic": r.positive}) for r in rows)
        + "\n"
    )
    models = tmp_path / "models.json"
    models.write_text(
        json.dumps(
            {
                "text": {
                    "id": "example/model",
                    "revision": "a" * 40,
                    "label_map": {"toxic": "toxicity"},
                }
            }
        )
    )

    def scorer(model: TextModelSettings, batch: list[Row]) -> list[float | None]:
        assert model.id == "example/model"
        return [by_text[row.text] for row in batch]

    policy_out, report_out = tmp_path / "policy.json", tmp_path / "report.md"
    result = run(models, data, policy_out, report_out, "mine", scorer)
    assert load_policies(policy_out)["mine"].version.startswith("calibrated-")
    page = report_out.read_text()
    for heading in (
        "## Policy rule",
        "## Held-out result",
        "## What it means when toxic text is rare",
        "## Before you rely on it",
    ):
        assert heading in page
    assert ("met" in page) and not result["target_met"]
    with pytest.raises(FileExistsError):
        run(models, data, policy_out, tmp_path / "other.md", "mine", scorer)


def test_the_policy_version_follows_the_data() -> None:
    rows, scores = separable()
    a = choose(rows, scores, "toxicity", "default", "1" * 64)["policy"]["policies"][0][
        "version"
    ]
    b = choose(rows, scores, "toxicity", "default", "2" * 64)["policy"]["policies"][0][
        "version"
    ]
    assert a != b
