from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modera.schemas import PolicyName

Category = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_./-]*$")
]
Version = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PolicyVersion(ResultModel):
    id: PolicyName
    version: Version


class ModelVersion(ResultModel):
    id: Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S+$")]
    revision: Version


class Decision(ResultModel):
    action: Literal["allow", "review", "block"]
    reasons: list[Category]

    @model_validator(mode="after")
    def validate_reasons(self) -> Self:
        if self.action == "allow" and self.reasons:
            raise ValueError("allow decisions must not contain reasons")
        if self.action != "allow" and not self.reasons:
            raise ValueError("review and block decisions require reasons")
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError("decision reasons must be unique")
        return self


class Finding(ResultModel):
    category: Category
    score: Score
    source: Literal["model", "rules"] = "model"


class Coverage(ResultModel):
    evaluated: Annotated[list[Category], Field(min_length=1)]
    unsupported: list[Category]
    not_applicable: list[Category]

    @model_validator(mode="after")
    def validate_categories(self) -> Self:
        categories = self.evaluated + self.unsupported + self.not_applicable
        if len(categories) != len(set(categories)):
            raise ValueError("coverage categories must be unique and disjoint")
        return self


class ModerationResult(ResultModel):
    request_id: Annotated[str, Field(pattern=r"^req_[0-9a-f]{32}$")]
    input_type: Literal["text", "image"]
    policy: PolicyVersion
    decision: Decision
    findings: Annotated[list[Finding], Field(min_length=1)]
    coverage: Coverage
    models: list[ModelVersion]
    rules_version: Version | None = None
    fallback: bool = False
    preprocessing_version: Version

    @model_validator(mode="after")
    def validate_findings(self) -> Self:
        if not self.models and self.rules_version is None:
            raise ValueError("a result requires model or rules provenance")
        if (
            any(f.source == "rules" for f in self.findings)
            and self.rules_version is None
        ):
            raise ValueError("rules findings require a rules version")
        if any(f.source == "model" for f in self.findings) and not self.models:
            raise ValueError("model findings require model provenance")
        if self.fallback and self.rules_version is None:
            raise ValueError("fallback requires rules provenance")
        categories = [finding.category for finding in self.findings]
        if len(categories) != len(set(categories)):
            raise ValueError("finding categories must be unique")
        if set(categories) != set(self.coverage.evaluated):
            raise ValueError("findings must match evaluated coverage exactly")
        if len(self.models) != len({model.id for model in self.models}):
            raise ValueError("model identifiers must be unique")
        return self
