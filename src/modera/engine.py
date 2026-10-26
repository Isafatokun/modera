import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from functools import partial

from modera.config import Settings
from modera.errors import APIError
from modera.inference import Classifier, InferenceWorker
from modera.policies import InputType, Policy, evaluate_policy
from modera.results import Coverage, ModelVersion, ModerationResult, PolicyVersion
from modera.rules import load_rules

logger = logging.getLogger(__name__)


class ModerationEngine:
    def __init__(self, settings: Settings, policies: Mapping[str, Policy]) -> None:
        settings = settings.model_copy(deep=True)
        self._settings = settings
        self._rules = load_rules(settings.text_rules_file)
        self._policies = policies
        if settings.text_fallback == "rules" and self._rules is None:
            raise ValueError("rules fallback requires a text rules file")
        self._fallback_policy = None
        if settings.text_fallback_policy is not None:
            self._fallback_policy = policies.get(settings.text_fallback_policy)
            available = (
                {rule.category for rule in self._rules.rules} if self._rules else set()
            )
            if (
                settings.text_fallback != "rules"
                or self._fallback_policy is None
                or not self._fallback_policy.text
                or any(
                    rule.category not in available
                    for rule in self._fallback_policy.text
                )
            ):
                raise ValueError(
                    "fallback policy must contain only configured text rules"
                )
        self._versions = {}
        for kind, model in (
            ("text", settings.text_model),
            ("image", settings.image_model),
        ):
            snapshot = {
                "model": model.model_dump(mode="json", exclude={"cache_dir"})
                if model
                else None,
                "rules": self._rules.model_dump(mode="json")
                if kind == "text" and self._rules
                else None,
            }
            digest = hashlib.sha256(
                json.dumps(snapshot, sort_keys=True).encode()
            ).hexdigest()
            self._versions[kind] = f"{kind}-v1.{digest}"
        self._text: Classifier | None = None
        self._image: Classifier | None = None
        self._workers = {
            kind: InferenceWorker(settings.inference_timeout_seconds)
            for kind in ("text", "image")
        }

    def enabled(self, input_type: InputType) -> bool:
        return bool(
            self._settings.image_model
            if input_type == "image"
            else self._settings.text_model or self._rules
        )

    async def start(self) -> None:
        kinds: tuple[InputType, ...] = ("text", "image")
        if any(self.enabled(kind) for kind in kinds) and not self._policies:
            raise ValueError("moderation backends require a policy file")
        for kind, selection in (
            ("text", self._settings.text_model),
            ("image", self._settings.image_model),
        ):
            if selection is None:
                continue
            if not selection.label_map or any(
                c.startswith("rules.") for c in selection.label_map.values()
            ):
                raise ValueError("models require explicit non-rules category mappings")
            try:
                adapter = await asyncio.to_thread(
                    Classifier, selection, self._settings.inference_threads
                )
            except Exception as exc:
                if (
                    kind == "text"
                    and self._settings.text_fallback == "rules"
                    and isinstance(exc, (OSError, ImportError))
                ):
                    logger.warning(
                        "Text model unavailable; rules fallback enabled: type=%s",
                        type(exc).__name__,
                    )
                    continue
                raise RuntimeError(
                    "configured classifier could not be loaded or warmed"
                ) from None
            if kind == "text":
                self._text = adapter
            else:
                self._image = adapter

    async def evaluate(
        self,
        input_type: InputType,
        content: str | bytes,
        policy: Policy,
        request_id: str,
    ) -> ModerationResult:
        selection = (
            self._settings.text_model
            if input_type == "text"
            else self._settings.image_model
        )
        adapter = self._text if input_type == "text" else self._image
        rules = self._rules if input_type == "text" else None
        findings = (
            rules.evaluate(content)
            if rules is not None and isinstance(content, str)
            else []
        )
        capabilities = {finding.category for finding in findings}
        if selection is not None:
            capabilities.update(selection.label_map.values())
        policy_rules = policy.rules_for(input_type)
        if any(
            rule.required and rule.category not in capabilities for rule in policy_rules
        ):
            raise APIError(
                400,
                "policy_unsupported",
                "A required policy check cannot evaluate this input.",
                param="policy",
            )
        if not any(rule.category in capabilities for rule in policy_rules):
            raise APIError(
                400,
                "policy_not_applicable",
                "No policy checks apply to this input.",
                param="policy",
            )
        models: list[ModelVersion] = []
        fallback = False
        if selection is not None and any(
            rule.category in selection.label_map.values() for rule in policy_rules
        ):
            try:
                if adapter is None:
                    raise APIError(
                        503,
                        "check_unavailable",
                        "The text model is unavailable.",
                        retryable=True,
                        headers={"Retry-After": "1"},
                    )
                model_findings = await self._workers[input_type].run(
                    partial(adapter.predict, content)
                )
                findings.extend(model_findings)
                models.append(
                    ModelVersion(id=selection.id, revision=selection.revision)
                )
            except APIError as exc:
                if (
                    input_type != "text"
                    or self._settings.text_fallback != "rules"
                    or exc.status_code != 503
                ):
                    raise
                fallback = True
                if self._fallback_policy is not None:
                    policy = self._fallback_policy
        coverage = Coverage(
            evaluated=[finding.category for finding in findings],
            unsupported=[
                rule.category
                for rule in policy.rules_for(input_type)
                if rule.category not in capabilities
            ],
            not_applicable=[],
        )
        decision = evaluate_policy(policy, input_type, findings, coverage)
        return ModerationResult(
            request_id=request_id,
            input_type=input_type,
            policy=PolicyVersion(id=policy.id, version=policy.version),
            decision=decision,
            findings=findings,
            coverage=coverage,
            models=models,
            rules_version=rules.version if rules else None,
            fallback=fallback,
            preprocessing_version=self._versions[input_type],
        )

    def close(self) -> None:
        for worker in self._workers.values():
            worker.close()
        self._text = None
        self._image = None
