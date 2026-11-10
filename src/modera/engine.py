import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from functools import partial
from typing import cast

from PIL import Image

from modera.config import Settings
from modera.errors import APIError
from modera.inference import (
    InferenceWorker,
    InvalidModelOutput,
    ModelAdapter,
    PreparedImage,
    create_adapter,
)
from modera.input_types import IMAGE, TEXT, InputType
from modera.model_config import ImageSettings, TextModelSettings
from modera.policies import Policy, evaluate_policy
from modera.results import (
    Coverage,
    Finding,
    ModelVersion,
    ModerationResult,
    PolicyVersion,
)
from modera.rules import TextRules, load_rules
from modera.workers import WorkerCapacity

logger = logging.getLogger(__name__)


def resolve_fallback_policy(
    settings: Settings, rules: TextRules | None, policies: Mapping[str, Policy]
) -> Policy | None:
    if settings.text_fallback == "rules" and rules is None:
        raise ValueError("rules fallback requires a text rules file")
    if settings.text_fallback_policy is None:
        return None
    policy = policies.get(settings.text_fallback_policy)
    available = {rule.category for rule in rules.rules} if rules else set()
    if (
        settings.text_fallback != "rules"
        or policy is None
        or not policy.text
        or any(rule.category not in available for rule in policy.text)
    ):
        raise ValueError("fallback policy must contain only configured text rules")
    return policy


def configuration_version(
    kind: InputType, settings: Settings, rules: TextRules | None
) -> str:
    model = settings.text_model if kind == TEXT else settings.image_model
    snapshot: dict[str, object] = {
        "model": model.model_dump(mode="json", exclude={"cache_dir"})
        if model
        else None,
        "rules": rules.model_dump(mode="json") if kind == TEXT and rules else None,
    }
    if kind == IMAGE and settings.image_models:
        snapshot = {
            "models": [
                image.model_dump(mode="json", exclude={"cache_dir"})
                for image in settings.image_models
            ]
        }
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return f"{kind}-v1.{digest}"


class ModerationEngine:
    def __init__(self, settings: Settings, policies: Mapping[str, Policy]) -> None:
        settings = settings.model_copy(deep=True)
        self._settings = settings
        self._rules = load_rules(settings.text_rules_file)
        self._policies = policies
        self._fallback_policy = resolve_fallback_policy(settings, self._rules, policies)
        self._versions = {
            kind: configuration_version(kind, settings, self._rules)
            for kind in (TEXT, IMAGE)
        }
        self._text: ModelAdapter | None = None
        self._image: ModelAdapter | None = None
        self._workers = {
            kind: InferenceWorker(
                settings.inference_timeout_seconds,
                settings.image_queue_size if kind == IMAGE else 0,
                settings.image_queue_wait_seconds,
            )
            for kind in (TEXT, IMAGE)
        }

        self._image_adapters: dict[str, ModelAdapter] = {}
        self._image_workers = {
            model.id: self._workers[IMAGE]
            if index == 0
            else InferenceWorker(
                settings.inference_timeout_seconds,
                settings.image_queue_size,
                settings.image_queue_wait_seconds,
            )
            for index, model in enumerate(settings.configured_image_models)
        }

    def enabled(self, input_type: InputType) -> bool:
        return bool(
            self._settings.configured_image_models
            if input_type == IMAGE
            else self._settings.text_model or self._rules
        )

    def worker_capacity(self, input_type: InputType) -> WorkerCapacity:
        if input_type == IMAGE and self._image_workers:
            capacities = [worker.capacity for worker in self._image_workers.values()]
            return WorkerCapacity(
                sum(c.active for c in capacities), sum(c.waiting for c in capacities)
            )
        return self._workers[input_type].capacity

    @property
    def image_preprocessor(self) -> Callable[[Image.Image], PreparedImage] | None:
        preprocessor = getattr(self._image, "prepare_image", None)
        return (
            cast(Callable[[Image.Image], PreparedImage], preprocessor)
            if callable(preprocessor)
            else None
        )

    async def start(self) -> None:
        kinds: tuple[InputType, ...] = (TEXT, IMAGE)
        if any(self.enabled(kind) for kind in kinds) and not self._policies:
            raise ValueError("moderation backends require a policy file")
        selections: list[tuple[InputType, TextModelSettings | ImageSettings | None]] = [
            (TEXT, self._settings.text_model),
            *((IMAGE, model) for model in self._settings.configured_image_models),
        ]
        for kind, selection in selections:
            if selection is None:
                continue
            if not selection.label_map or any(
                c.startswith("rules.") for c in selection.label_map.values()
            ):
                raise ValueError("models require explicit non-rules category mappings")
            try:
                adapter = await (
                    self._image_workers[selection.id]
                    if kind == IMAGE
                    else self._workers[TEXT]
                ).initialize(
                    partial(create_adapter, selection, self._settings.inference_threads)
                )
            except Exception as exc:
                if (
                    kind == TEXT
                    and self._settings.text_fallback == "rules"
                    and isinstance(exc, (OSError, ImportError))
                ):
                    logger.warning(
                        "Text model unavailable; rules fallback enabled: type=%s",
                        type(exc).__name__,
                    )
                    continue
                raise RuntimeError(
                    "configured model could not be loaded or warmed"
                ) from None
            if kind == TEXT:
                self._text = adapter
            else:
                self._image_adapters[selection.id] = adapter
                if len(self._settings.configured_image_models) == 1:
                    self._image = adapter

    async def evaluate(
        self,
        input_type: InputType,
        content: str | bytes | PreparedImage,
        policy: Policy,
        request_id: str,
    ) -> ModerationResult:
        selections = self._selections(input_type)
        rules = self._rules if input_type == TEXT else None
        findings = (
            rules.evaluate(content)
            if rules is not None and isinstance(content, str)
            else []
        )
        capabilities = {finding.category for finding in findings}
        for selection in selections:
            capabilities.update(selection.label_map.values())
        needed = self._require_applicable(policy, input_type, capabilities)
        model_findings, models, fallback = await self._run_models(
            input_type, selections, needed, content
        )
        findings.extend(model_findings)
        if fallback and self._fallback_policy is not None:
            policy = self._fallback_policy
        coverage = Coverage(
            evaluated=[finding.category for finding in findings],
            unsupported=sorted(policy.categories_for(input_type) - capabilities),
            not_applicable=[],
        )
        return ModerationResult(
            request_id=request_id,
            input_type=input_type,
            policy=PolicyVersion(id=policy.id, version=policy.version),
            decision=evaluate_policy(policy, input_type, findings, coverage),
            findings=findings,
            coverage=coverage,
            models=models,
            rules_version=rules.version if rules else None,
            fallback=fallback,
            preprocessing_version=self._versions[input_type],
        )

    def _selections(
        self, input_type: InputType
    ) -> tuple[TextModelSettings | ImageSettings, ...]:
        if input_type == IMAGE:
            return self._settings.configured_image_models
        return (self._settings.text_model,) if self._settings.text_model else ()

    @staticmethod
    def _require_applicable(
        policy: Policy, input_type: InputType, capabilities: set[str]
    ) -> frozenset[str]:
        if policy.categories_for(input_type, required_only=True) - capabilities:
            raise APIError(
                400,
                "policy_unsupported",
                "A required policy check cannot evaluate this input.",
                param="policy",
            )
        needed = policy.categories_for(input_type)
        if not needed.intersection(capabilities):
            raise APIError(
                400,
                "policy_not_applicable",
                "No policy checks apply to this input.",
                param="policy",
            )
        return needed

    async def _run_models(
        self,
        input_type: InputType,
        selections: tuple[TextModelSettings | ImageSettings, ...],
        needed: frozenset[str],
        content: str | bytes | PreparedImage,
    ) -> tuple[list[Finding], list[ModelVersion], bool]:
        findings: list[Finding] = []
        models: list[ModelVersion] = []
        fallback = False
        try:
            # One overall inference budget, rather than one full budget per model.
            async with asyncio.timeout(
                self._settings.inference_timeout_seconds
                if input_type == IMAGE
                else None
            ):
                for selection in selections:
                    if not needed.intersection(selection.label_map.values()):
                        continue
                    try:
                        findings.extend(
                            await self._run_model(
                                input_type, selection, content, len(selections) > 1
                            )
                        )
                        models.append(
                            ModelVersion(id=selection.id, revision=selection.revision)
                        )
                    except APIError as exc:
                        if not self._falls_back(input_type, exc):
                            raise
                        fallback = True
        except TimeoutError as exc:
            raise APIError(
                503,
                "check_unavailable",
                "Model evaluation exceeded its deadline.",
                retryable=True,
                headers={"Retry-After": "1"},
            ) from exc
        return findings, models, fallback

    async def _run_model(
        self,
        input_type: InputType,
        selection: TextModelSettings | ImageSettings,
        content: str | bytes | PreparedImage,
        attribute: bool,
    ) -> list[Finding]:
        adapter = (
            self._image_adapters.get(selection.id)
            if input_type == IMAGE
            else self._text
        )
        worker = (
            self._image_workers[selection.id]
            if input_type == IMAGE
            else self._workers[TEXT]
        )
        if adapter is None:
            raise APIError(
                503,
                "check_unavailable",
                "The model is unavailable.",
                retryable=True,
                headers={"Retry-After": "1"},
            )
        if not attribute:
            return await worker.run(partial(adapter.predict, content))
        found = await worker.run(
            partial(self._predict_image, adapter, content, selection)
        )
        return [finding.model_copy(update={"model": selection.id}) for finding in found]

    def _falls_back(self, input_type: InputType, error: APIError) -> bool:
        return (
            input_type == TEXT
            and self._settings.text_fallback == "rules"
            and error.status_code == 503
            and error.code == "check_unavailable"
        )

    @staticmethod
    def _predict_image(
        adapter: ModelAdapter,
        content: str | bytes | PreparedImage,
        selection: TextModelSettings | ImageSettings,
    ) -> list[Finding]:
        findings = adapter.predict(content)
        if (
            len(findings) != len(selection.label_map)
            or {finding.category for finding in findings}
            != set(selection.label_map.values())
            or any(finding.source != "model" for finding in findings)
        ):
            raise InvalidModelOutput(
                "model evidence does not match configured categories"
            )
        return findings

    def close(self) -> None:
        for worker in set(self._workers.values()) | set(self._image_workers.values()):
            worker.close()
        self._text = None
        self._image = None
        self._image_adapters.clear()
