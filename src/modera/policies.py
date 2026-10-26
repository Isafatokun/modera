from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from modera.errors import APIError
from modera.results import Category, Coverage, Decision, Finding, Score, Version
from modera.schemas import PolicyName

InputType = Literal["text", "image"]


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PolicyRule(PolicyModel):
    category: Category
    required: bool = True
    review_threshold: Score | None = None
    block_threshold: Score | None = None

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        if self.review_threshold is None and self.block_threshold is None:
            raise ValueError("a rule requires a review or block threshold")
        if (
            self.review_threshold is not None
            and self.block_threshold is not None
            and self.review_threshold >= self.block_threshold
        ):
            raise ValueError("review threshold must be below block threshold")
        return self


class Policy(PolicyModel):
    id: PolicyName
    version: Version
    text: tuple[PolicyRule, ...] = ()
    image: tuple[PolicyRule, ...] = ()

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        if not self.text and not self.image:
            raise ValueError("a policy must define text or image rules")
        for rules in (self.text, self.image):
            if len(rules) != len({rule.category for rule in rules}):
                raise ValueError(
                    "rule categories must be unique within each input type"
                )
        return self

    def rules_for(self, input_type: InputType) -> tuple[PolicyRule, ...]:
        match input_type:
            case "text":
                return self.text
            case "image":
                return self.image
            case _:
                raise ValueError("unsupported input type")


class PolicyConfig(PolicyModel):
    policies: Annotated[tuple[Policy, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_identifiers(self) -> Self:
        if len(self.policies) != len({policy.id for policy in self.policies}):
            raise ValueError("policy identifiers must be unique")
        return self


def load_policies(path: Path | None) -> Mapping[str, Policy]:
    if path is None:
        return MappingProxyType({})
    try:
        with path.open("rb") as source:
            content = source.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("policy configuration exceeds 1 MiB")
        config = PolicyConfig.model_validate_json(content)
    except (OSError, ValidationError):
        raise ValueError(
            "policy configuration could not be loaded or is invalid"
        ) from None
    return MappingProxyType({policy.id: policy for policy in config.policies})


def resolve_policy(
    policies: Mapping[str, Policy], name: str, input_type: InputType
) -> Policy:
    policy = policies.get(name)
    if policy is None:
        raise APIError(
            400,
            "unknown_policy",
            "The selected policy is not configured.",
            param="policy",
        )
    if not policy.rules_for(input_type):
        raise APIError(
            400,
            "policy_not_applicable",
            "The policy does not support this input type.",
            param="policy",
        )
    return policy


def evaluate_policy(
    policy: Policy,
    input_type: InputType,
    findings: Sequence[Finding],
    coverage: Coverage,
) -> Decision:
    scores = {finding.category: finding.score for finding in findings}
    if len(scores) != len(findings) or set(scores) != set(coverage.evaluated):
        raise ValueError("findings must match evaluated coverage exactly")
    rules = policy.rules_for(input_type)
    if not rules:
        raise APIError(
            400,
            "policy_not_applicable",
            "The policy does not support this input type.",
            param="policy",
        )

    for rule in rules:
        if rule.category in scores:
            continue
        if (
            rule.category in coverage.unsupported
            or rule.category in coverage.not_applicable
        ):
            if rule.required:
                raise APIError(
                    400,
                    "policy_unsupported",
                    "A required policy check cannot evaluate this input.",
                    param="policy",
                )
            continue
        raise APIError(
            503,
            "check_unavailable",
            "A policy check did not complete.",
            retryable=True,
            headers={"Retry-After": "2"},
        )

    applicable = [rule for rule in rules if rule.category in scores]
    if not applicable:
        raise APIError(
            400,
            "policy_not_applicable",
            "No policy checks apply to this input.",
            param="policy",
        )
    blocked = [
        rule.category
        for rule in applicable
        if rule.block_threshold is not None
        and scores[rule.category] >= rule.block_threshold
    ]
    if blocked:
        return Decision(action="block", reasons=blocked)
    reviewed = [
        rule.category
        for rule in applicable
        if rule.review_threshold is not None
        and scores[rule.category] >= rule.review_threshold
    ]
    if reviewed:
        return Decision(action="review", reasons=reviewed)
    return Decision(action="allow", reasons=[])
