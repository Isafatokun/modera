from pathlib import Path

import pytest
from pydantic import ValidationError

from modera.rules import TextRules, load_rules


def rules(phrases: list[str]) -> TextRules:
    import json

    return TextRules.model_validate_json(
        json.dumps(
            {
                "version": "1",
                "rules": [{"category": "rules.blocklist", "phrases": phrases}],
            }
        )
    )


def test_rules_normalize_case_unicode_and_whitespace() -> None:
    configured = rules(["blocked phrase"])
    assert (
        configured.evaluate(
            "a \uff22\uff2c\uff2f\uff23\uff2b\uff25\uff24\n PHRASE appears"
        )[0].score
        == 1
    )
    assert configured.evaluate("ordinary text")[0].score == 0
    assert configured.evaluate("ordinary text")[0].source == "rules"


def test_rules_are_literal_substrings_not_regex() -> None:
    configured = rules(["a.*b"])
    assert configured.evaluate("a.*b")[0].score == 1
    assert configured.evaluate("axxxb")[0].score == 0


@pytest.mark.parametrize(
    "phrases", [[], [" "], ["same", "\uff33\uff21\uff2d\uff25"], ["a"] * 1001]
)
def test_invalid_rule_configuration_is_rejected(phrases: list[str]) -> None:
    with pytest.raises(ValidationError):
        rules(phrases)


def test_rules_cannot_claim_model_categories() -> None:
    with pytest.raises(ValidationError):
        TextRules.model_validate_json(
            '{"version":"1","rules":[{"category":"toxicity","phrases":["bad"]}]}'
        )


def test_invalid_or_missing_rule_files_fail(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    with pytest.raises(ValueError):
        load_rules(path)
    path.write_text("{}")
    with pytest.raises(ValueError):
        load_rules(path)
    assert load_rules(None) is None
