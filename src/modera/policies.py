from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from modera.errors import APIError
from modera.input_types import IMAGE
from modera.input_types import InputType as InputType
from modera.results import Category, Coverage, Decision, Finding, Score, Version
from modera.schemas import PolicyName


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


class ScoreCondition(PolicyModel):
    category: Category
    min: Score


class ImageComposite(PolicyModel):
    id: Category
    all_of: Annotated[tuple[ScoreCondition, ...], Field(max_length=16)] = ()
    any_of: Annotated[tuple[ScoreCondition, ...], Field(max_length=16)] = ()
    scope: Literal["image"] = "image"
    action: Literal["review", "block"]

    @model_validator(mode="after")
    def validate_conditions(self) -> Self:
        if bool(self.all_of) == bool(self.any_of):
            raise ValueError("choose exactly one non-empty all_of or any_of")
        conditions = self.all_of or self.any_of
        if len({condition.category for condition in conditions}) != len(conditions):
            raise ValueError("composite conditions require unique categories")
        return self

    @property
    def conditions(self) -> tuple[ScoreCondition, ...]:
        return self.all_of or self.any_of


class Policy(PolicyModel):
    id: PolicyName
    version: Version
    text: tuple[PolicyRule, ...] = ()
    image: tuple[PolicyRule, ...] = ()
    image_composites: Annotated[tuple[ImageComposite, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        if not self.text and not self.image and not self.image_composites:
            raise ValueError("a policy must define text or image rules")
        for rules in (self.text, self.image):
            if len(rules) != len({rule.category for rule in rules}):
                raise ValueError(
                    "rule categories must be unique within each input type"
                )
        if len({rule.id for rule in self.image_composites}) != len(
            self.image_composites
        ):
            raise ValueError("composite IDs must be unique")
        return self

    def categories_for(
        self, input_type: InputType, *, required_only: bool = False
    ) -> frozenset[str]:
        categories = {
            rule.category
            for rule in self.rules_for(input_type)
            if not required_only or rule.required
        }
        if input_type == IMAGE:
            categories.update(
                condition.category
                for rule in self.image_composites
                for condition in rule.conditions
            )
        return frozenset(categories)

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
    if not policy.categories_for(input_type):
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
    if not policy.categories_for(input_type):
        raise APIError(
            400,
            "policy_not_applicable",
            "The policy does not support this input type.",
            param="policy",
        )

    required = policy.categories_for(input_type, required_only=True)
    for category in policy.categories_for(input_type):
        if category in scores:
            continue
        if category in coverage.unsupported or category in coverage.not_applicable:
            if category in required:
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

    composites = policy.image_composites if input_type == IMAGE else ()
    matched = []
    for composite in composites:
        conditions = [scores[c.category] >= c.min for c in composite.conditions]
        fires = all(conditions) if composite.all_of else any(conditions)
        if fires:
            matched.append(composite)
    applicable = [rule for rule in rules if rule.category in scores]
    if not applicable and not composites:
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
    reviewed = [
        rule.category
        for rule in applicable
        if rule.review_threshold is not None
        and scores[rule.category] >= rule.review_threshold
    ]
    outcomes: tuple[tuple[Literal["block", "review"], list[str]], ...] = (
        ("block", blocked),
        ("review", reviewed),
    )
    for action, reasons in outcomes:
        matches = [rule for rule in matched if rule.action == action]
        reasons.extend(
            condition.category
            for rule in matches
            for condition in rule.conditions
            if scores[condition.category] >= condition.min
        )
        if reasons:
            return Decision(
                action=action,
                reasons=list(dict.fromkeys(reasons)),
                matched_rules=[rule.id for rule in matches],
            )
    return Decision(action="allow", reasons=[])
