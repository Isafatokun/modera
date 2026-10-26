import unicodedata
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from modera.results import Finding, Version


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


class LiteralRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    category: Annotated[
        str, Field(max_length=64, pattern=r"^rules\.[a-z][a-z0-9_./-]*$")
    ]
    phrases: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=256)], ...],
        Field(min_length=1, max_length=1000),
    ]

    @model_validator(mode="after")
    def valid_phrases(self) -> Self:
        values = [normalize(phrase) for phrase in self.phrases]
        if any(not value for value in values) or len(values) != len(set(values)):
            raise ValueError("phrases must be nonblank and unique after normalization")
        return self


class TextRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Version
    rules: Annotated[tuple[LiteralRule, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_categories(self) -> Self:
        if len(self.rules) != len({rule.category for rule in self.rules}):
            raise ValueError("rule categories must be unique")
        if sum(len(rule.phrases) for rule in self.rules) > 1000:
            raise ValueError("at most 1000 phrases are supported")
        return self

    def evaluate(self, text: str) -> list[Finding]:
        cleaned = normalize(text)
        return [
            Finding(
                category=rule.category,
                score=1.0
                if any(normalize(p) in cleaned for p in rule.phrases)
                else 0.0,
                source="rules",
            )
            for rule in self.rules
        ]


def load_rules(path: Path | None) -> TextRules | None:
    if path is None:
        return None
    try:
        with path.open("rb") as file:
            content = file.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("rules configuration exceeds 1 MiB")
        return TextRules.model_validate_json(content)
    except (OSError, ValidationError):
        raise ValueError("text rules configuration is invalid or unreadable") from None
