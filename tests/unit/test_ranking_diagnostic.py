import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evaluation.diagnose import Prediction, diagnose, main, roc_auc
from evaluation.report import Case


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([(True, 0.9), (False, 0.1)], 1.0),
        ([(True, 0.1), (False, 0.9)], 0.0),
        ([(True, 0.5), (False, 0.5)], 0.5),
        ([(True, 0.8), (False, 0.5), (True, 0.4), (False, 0.3)], 0.75),
        ([(True, 0.2), (False, 0.2), (True, 0.8), (False, 0.5)], 0.625),
        ([], None),
        ([(True, 0.1)], None),
        ([(False, 0.5)], None),
    ],
)
def test_auc_handles_ranking_ties_and_missing_classes(
    rows: list[tuple[bool, float]], expected: float | None
) -> None:
    assert roc_auc(rows) == expected
    assert roc_auc(list(reversed(rows))) == expected


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1])
def test_auc_rejects_invalid_scores(score: float) -> None:
    with pytest.raises(ValueError):
        roc_auc([(True, score), (False, 0.1)])


def sample(identifier: str, label: str, language: str = "en") -> Case:
    return Case.model_validate(
        {
            "id": identifier,
            "group": identifier,
            "split": "test",
            "input_type": "text",
            "slice": language,
            "expected": label,
        }
    )


def prediction(identifier: str, score: float) -> Prediction:
    return Prediction(
        id=identifier, scores={"toxicity": score}, action="allow", error=None
    )


def test_failed_cases_do_not_look_clean_or_get_hidden_by_macro_average() -> None:
    cases = [
        sample("a", "block"),
        sample("b", "allow"),
        sample("c", "block", "fr"),
        sample("d", "allow", "fr"),
    ]
    observations = [
        prediction("a", 0.9),
        prediction("b", 0.1),
        prediction("c", 0.8),
        Prediction(id="d", scores=None, action=None, error="input_too_long"),
    ]
    result = diagnose(cases, observations)
    assert result["macro_language_roc_auc_completed"] is None
    assert result["undefined_languages"] == ["fr"]
    assert result["languages"]["en"]["roc_auc_completed"] == 1
    assert result["languages"]["fr"]["non_toxic_failed"] == 1


@pytest.mark.parametrize(
    "rows", [[], [prediction("a", 0.5), prediction("a", 0.6)], [prediction("b", 0.4)]]
)
def test_predictions_must_match_cases_once(rows: list[Prediction]) -> None:
    with pytest.raises(ValueError):
        diagnose([sample("a", "block")], rows)


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "a", "scores": None, "action": "allow", "error": None},
        {
            "id": "a",
            "scores": {"toxicity": float("nan")},
            "action": "allow",
            "error": None,
        },
        {"id": "a", "scores": {"other": 0.5}, "action": "allow", "error": None},
        {"id": "a", "scores": None, "action": "allow", "error": "unavailable"},
    ],
)
def test_predictions_are_validated(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Prediction.model_validate(payload)


def test_cli_checks_fingerprints_before_reading_predictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    data, run = tmp_path / "data", tmp_path / "run"
    data.mkdir()
    run.mkdir()
    (data / "cases.json").write_text("[]")
    (data / "protocol.json").write_text(json.dumps({"cases_sha256": "wrong"}))
    (run / "report.json").write_text("{}")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "diagnose",
            "--directory",
            str(data),
            "--run",
            str(run),
            "--output",
            str(tmp_path / "output.json"),
        ],
    )
    with pytest.raises(ValueError, match="frozen sample"):
        main()
    assert not (tmp_path / "output.json").exists()


@pytest.mark.parametrize("tamper", ["protocol", "predictions", None])
def test_cli_verifies_evidence_and_writes_only_aggregate_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str | None
) -> None:
    import sys

    from evaluation.compare_text import digest_file

    data, run = tmp_path / "data", tmp_path / "run"
    data.mkdir()
    run.mkdir()
    (data / "cases.json").write_text(
        json.dumps(
            [
                {
                    "case": sample("a", "block").model_dump(),
                    "text": "private positive text",
                },
                {
                    "case": sample("b", "allow").model_dump(),
                    "text": "private negative text",
                },
            ]
        )
    )
    protocol = {"cases_sha256": digest_file(data / "cases.json")}
    (data / "protocol.json").write_text(json.dumps(protocol))
    (run / "predictions.jsonl").write_text(
        "\n".join(
            row.model_dump_json()
            for row in [prediction("a", 0.9), prediction("b", 0.1)]
        )
        + "\n"
    )
    report = {
        "protocol": protocol,
        "predictions_sha256": digest_file(run / "predictions.jsonl"),
        "candidate": {"id": "example/text", "revision": "1"},
    }
    (run / "report.json").write_text(json.dumps(report))
    if tamper == "protocol":
        (data / "protocol.json").write_text(json.dumps({**protocol, "changed": True}))
    elif tamper == "predictions":
        with (run / "predictions.jsonl").open("a") as handle:
            handle.write("\n")
    output = tmp_path / "diagnostic.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "diagnose",
            "--directory",
            str(data),
            "--run",
            str(run),
            "--output",
            str(output),
        ],
    )
    if tamper:
        with pytest.raises(ValueError, match=r"differ|changed"):
            main()
        assert not output.exists()
    else:
        main()
        assert json.loads(output.read_text())["macro_language_roc_auc_completed"] == 1
        assert "private" not in output.read_text()
        with pytest.raises(FileExistsError):
            main()
