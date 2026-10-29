import hashlib
import json
from pathlib import Path

import pytest

from evaluation.compare_text import digest_file
from evaluation.external_text import deduplicate, evaluate, labelled_rows, write
from evaluation.photo_challenge import verified_blob
from modera import engine
from modera.model_config import ImageModelSettings, TextModelSettings
from modera.results import Finding


def test_conflicting_and_duplicate_source_texts_cannot_cross_splits() -> None:
    rows, counts = deduplicate(
        [
            {"text": "Same  text", "positive": True},
            {"text": "same text", "positive": True},
            {"text": "Conflict", "positive": True},
            {"text": "conflict", "positive": False},
        ]
    )
    assert len(rows) == 1
    assert counts == {"duplicate_rows_collapsed": 1, "conflicting_rows_excluded": 2}


def test_toxigen_uses_human_rating_instead_of_generator_intent(tmp_path: Path) -> None:
    path = tmp_path / "english.csv"
    path.write_text(
        "text,toxicity_human,toxicity_ai\nneutral,2.6666666667,5\nsubtle,3,1\n"
    )
    assert [row["positive"] for row in labelled_rows("toxigen", path)] == [False, True]
    path.write_text("text,toxicity_human\ninvalid,nan\n")
    with pytest.raises(ValueError, match="rating"):
        labelled_rows("toxigen", path)


@pytest.mark.parametrize("failure", [False, True])
def test_http_collection_freezes_proposals_before_test_and_retains_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    directory = tmp_path / "inputs"
    directory.mkdir()
    output = tmp_path / "results"
    candidate = tmp_path / "fixture.json"
    model = TextModelSettings(
        id="example/text", revision="a" * 40, label_map={"native": "toxicity"}
    )
    candidate.write_text(model.model_dump_json())
    cases, validations, tests = [], [], []
    for source in ("toxifrench", "toxigen"):
        for split in ("validation", "test"):
            for positive in (False, True):
                identifier = f"{source}-{split}-{positive}"
                text = f"{split}-{'harmful' if positive else 'ordinary'}-{source}"
                cases.append(
                    {
                        "id": identifier,
                        "group": identifier,
                        "split": split,
                        "slice": source,
                        "input_type": "text",
                        **(
                            {"expected": "block" if positive else "allow"}
                            if split == "validation"
                            else {}
                        ),
                    }
                )
                row = {"id": identifier, "text": text}
                if split == "validation":
                    validations.append(row)
                else:
                    tests.append({**row, "positive": positive, "source": source})
    write(directory / "cases.json", cases)
    write(directory / "validation-inputs.json", validations)
    write(directory / "sealed-test.json", tests)
    write(
        directory / "protocol.json",
        {
            "candidates": {"fixture": model.model_dump(mode="json")},
            "hashes": {
                name: digest_file(directory / name)
                for name in ("cases.json", "validation-inputs.json", "sealed-test.json")
            },
            "budget": {"max_false_positive_rate": 0.05, "min_recall": 0.9},
        },
    )

    class Classifier:
        def __init__(
            self, settings: TextModelSettings | ImageModelSettings, threads: int
        ) -> None:
            pass

        def predict(self, content: str) -> list[Finding]:
            if content.startswith("test"):
                assert (output / "toxifrench-proposal.json").exists()
                assert (output / "toxigen-proposal.json").exists()
                if failure and content == "test-harmful-toxigen":
                    raise RuntimeError("controlled inference failure")
            return [
                Finding(category="toxicity", score=0.9 if "harmful" in content else 0.1)
            ]

    monkeypatch.setattr(engine, "Classifier", Classifier)
    evaluate(directory, candidate, tmp_path / "cache", output)
    report = json.loads((output / "report.json").read_bytes())
    french = report["results"]["toxifrench"]["held_out_assessment"]
    english = report["results"]["toxigen"]["held_out_assessment"]
    assert french["status"] == "passed"
    assert english["status"] == ("incomplete" if failure else "passed")
    assert english["metrics"]["positive_failed"] == int(failure)
    # Per-source outputs must be independently consumable by the assessment CLI.
    proposal = json.loads((output / "toxigen-proposal.json").read_bytes())
    assert proposal["inputs"]["cases_sha256"] == digest_file(
        output / "toxigen-cases.json"
    )
    assert proposal["inputs"]["validation_results_sha256"] == digest_file(
        output / "toxigen-validation-results.jsonl"
    )
    with pytest.raises(FileExistsError):
        evaluate(directory, candidate, tmp_path / "cache", output)
    (directory / "sealed-test.json").write_text("[]")
    with pytest.raises(ValueError, match="frozen inputs"):
        evaluate(directory, candidate, tmp_path / "cache", tmp_path / "other-results")
    assert not (tmp_path / "other-results").exists()


def test_pinned_photo_bytes_are_verified_before_evaluation() -> None:
    data = b"image fixture"
    digest = hashlib.sha1(b"blob 13\0" + data).hexdigest()
    assert verified_blob(data, digest)
    assert not verified_blob(data + b"corruption", digest)
