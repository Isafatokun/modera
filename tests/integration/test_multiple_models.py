import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from threading import Event, get_ident

import pytest
from fastapi.testclient import TestClient

from modera import engine
from modera.config import Settings
from modera.inference import InvalidModelOutput, ModelUnavailable, PreparedImage
from modera.main import create_app
from modera.model_config import (
    ImageSettings,
    ModelConfig,
    TextModelSettings,
    load_models,
)
from modera.results import Finding, ModerationResult


def setup(tmp_path: Path) -> Settings:
    models = tmp_path / "models.json"
    models.write_text(
        json.dumps(
            {
                "image_models": [
                    {
                        "id": "example/detector",
                        "revision": "a" * 40,
                        "backend": "transformers_object_detection",
                        "cache_dir": "cache",
                        "label_map": {"native": "detect.exposed"},
                    },
                    {
                        "id": "example/classifier",
                        "revision": "b" * 40,
                        "label_map": {"native": "classify.sexual"},
                    },
                ]
            }
        )
    )
    policy = tmp_path / "policies.json"
    policy.write_text(
        json.dumps(
            {
                "policies": [
                    {
                        "id": "default",
                        "version": "1",
                        "image": [
                            {
                                "category": "detect.exposed",
                                "block_threshold": 0.8,
                                "review_threshold": 0.5,
                            },
                            {
                                "category": "classify.sexual",
                                "block_threshold": 0.8,
                                "review_threshold": 0.5,
                            },
                        ],
                    },
                    {
                        "id": "detector-only",
                        "version": "1",
                        "image": [
                            {"category": "detect.exposed", "block_threshold": 0.8},
                        ],
                    },
                    {
                        "id": "unsupported",
                        "version": "1",
                        "image": [
                            {"category": "missing", "block_threshold": 0.8},
                        ],
                    },
                ]
            }
        )
    )
    return Settings(model_file=models, policy_file=policy)


class FakeAdapter:
    def __init__(
        self, settings: TextModelSettings | ImageSettings, threads: int
    ) -> None:
        self.settings = settings
        self.categories = frozenset(settings.label_map.values())
        self.thread = get_ident()
        self.score = 0.1
        self.failure: Exception | None = None
        self.calls = 0
        self.release: Event | None = None
        self.empty = False

    def predict(self, content: str | bytes | PreparedImage) -> list[Finding]:
        assert get_ident() == self.thread
        assert isinstance(content, bytes)
        self.calls += 1
        if self.release is not None:
            assert self.release.wait(5)
        if self.failure is not None:
            raise self.failure
        return (
            []
            if self.empty
            else [Finding(category=c, score=self.score) for c in self.categories]
        )


def install(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeAdapter]:
    adapters: dict[str, FakeAdapter] = {}

    def factory(
        settings: TextModelSettings | ImageSettings, threads: int
    ) -> FakeAdapter:
        adapter = FakeAdapter(settings, threads)
        adapters[settings.id] = adapter
        return adapter

    monkeypatch.setattr(engine, "create_adapter", factory)
    return adapters


@pytest.mark.parametrize(
    "scores,action",
    [((0.9, 0.1), "block"), ((0.1, 0.6), "review"), ((0.1, 0.1), "allow")],
)
def test_models_keep_separate_evidence_and_share_one_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    image_bytes: bytes,
    scores: tuple[float, float],
    action: str,
) -> None:
    adapters = install(monkeypatch)
    with TestClient(create_app(setup(tmp_path)), headers=auth_headers) as client:
        for adapter, score in zip(adapters.values(), scores, strict=True):
            adapter.score = score
        response = client.post(
            "/v1/moderations", files={"file": ("photo.png", image_bytes)}
        )
        assert response.status_code == 200
        result = response.json()
        assert result["decision"]["action"] == action
        assert [f["score"] for f in result["findings"]] == list(scores)
        assert [f["model"] for f in result["findings"]] == list(adapters)
        assert [m["id"] for m in result["models"]] == list(adapters)
        assert all(a.calls == 1 for a in adapters.values())


@pytest.mark.parametrize(
    "failure,status",
    [
        (ModelUnavailable("private"), 503),
        (InvalidModelOutput("private"), 500),
        (RuntimeError("private"), 500),
        (None, 500),
    ],
)
def test_partial_evidence_never_becomes_a_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    image_bytes: bytes,
    failure: Exception | None,
    status: int,
) -> None:
    adapters = install(monkeypatch)
    with TestClient(create_app(setup(tmp_path)), headers=auth_headers) as client:
        adapters["example/detector"].score = 0.99
        adapters["example/classifier"].failure = failure
        adapters["example/classifier"].empty = failure is None
        response = client.post(
            "/v1/moderations", files={"file": ("photo.png", image_bytes)}
        )
        assert response.status_code == status
        assert response.json()["error"]["retryable"] == (status == 503)
        assert "decision" not in response.json()
        assert "private" not in response.text
        assert "modera_decisions_total{" not in client.get("/metrics").text


def test_policy_selects_needed_models_and_rejects_missing_support_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    image_bytes: bytes,
) -> None:
    adapters = install(monkeypatch)
    with TestClient(create_app(setup(tmp_path)), headers=auth_headers) as client:
        adapters["example/classifier"].failure = RuntimeError("must not run")
        response = client.post(
            "/v1/moderations",
            data={"policy": "detector-only"},
            files={"file": ("photo.png", image_bytes)},
        )
        assert response.status_code == 200
        assert len(response.json()["models"]) == 1
        assert response.json()["findings"][0]["model"] == "example/detector"
        assert adapters["example/classifier"].calls == 0
        response = client.post(
            "/v1/moderations",
            data={"policy": "unsupported"},
            files={"file": ("photo.png", image_bytes)},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "policy_unsupported"
        assert adapters["example/detector"].calls == 1


def test_timeout_retains_native_slot_but_other_model_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    image_bytes: bytes,
) -> None:
    adapters = install(monkeypatch)
    settings = setup(tmp_path).model_copy(update={"inference_timeout_seconds": 0.1})
    app = create_app(settings)
    release = Event()
    try:
        with TestClient(app, headers=auth_headers) as client:
            adapters["example/classifier"].release = release
            response = client.post(
                "/v1/moderations", files={"file": ("photo.png", image_bytes)}
            )
            assert response.status_code == 503
            assert "decision" not in response.json()
            assert app.state.engine.worker_capacity("image").active == 1
            response = client.post(
                "/v1/moderations",
                data={"policy": "detector-only"},
                files={"file": ("photo.png", image_bytes)},
            )
            assert response.status_code == 200
            release.set()

            async def idle() -> None:
                async with asyncio.timeout(2):
                    while app.state.engine.worker_capacity("image").active:
                        await asyncio.sleep(0.01)

            assert client.portal is not None
            client.portal.call(idle)
    finally:
        release.set()


@pytest.mark.parametrize("change", ["id", "category", "mixed", "empty", "limit"])
def test_ambiguous_model_configuration_fails(tmp_path: Path, change: str) -> None:
    setup(tmp_path)
    raw = json.loads((tmp_path / "models.json").read_text())
    if change == "id":
        raw["image_models"][1]["id"] = raw["image_models"][0]["id"]
    elif change == "category":
        raw["image_models"][1]["label_map"] = raw["image_models"][0]["label_map"]
    elif change == "mixed":
        raw["image"] = raw["image_models"][0]
    elif change == "empty":
        raw["image_models"][0]["label_map"] = {}
    else:
        raw["image_models"] *= 3
    with pytest.raises(ValueError):
        ModelConfig.model_validate_json(json.dumps(raw))


def test_model_file_resolves_every_cache_relative_to_file(tmp_path: Path) -> None:
    setup(tmp_path)
    config = load_models(tmp_path / "models.json")
    assert config.configured_images[0].cache_dir == tmp_path / "cache"
    assert config.configured_images[1].cache_dir is None


def test_composite_only_policy_selects_both_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    image_bytes: bytes,
) -> None:
    adapters = install(monkeypatch)
    settings = setup(tmp_path)
    path = tmp_path / "policies.json"
    raw = json.loads(path.read_text())
    raw["policies"][0]["image"] = []
    raw["policies"][0]["image_composites"] = [
        {
            "id": "signals-together",
            "action": "review",
            "all_of": [
                {"category": "detect.exposed", "min": 0.5},
                {"category": "classify.sexual", "min": 0.6},
            ],
        }
    ]
    path.write_text(json.dumps(raw))
    with TestClient(create_app(settings), headers=auth_headers) as client:
        for adapter in adapters.values():
            adapter.score = 0.7
        response = client.post(
            "/v1/moderations", files={"file": ("photo.png", image_bytes)}
        )
        assert response.status_code == 200
        result = response.json()
        assert result["decision"]["action"] == "review"
        assert result["decision"]["matched_rules"] == ["signals-together"]
        assert len(result["findings"]) == 2
        assert all(adapter.calls == 1 for adapter in adapters.values())


def test_one_inference_deadline_covers_both_model_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    image_bytes: bytes,
) -> None:
    adapters = install(monkeypatch)
    settings = setup(tmp_path).model_copy(update={"inference_timeout_seconds": 0.3})
    app = create_app(settings)
    with TestClient(app, headers=auth_headers) as client:
        for worker in app.state.engine._image_workers.values():
            original = worker.run

            async def delayed_dispatch(
                operation: Callable[[], list[Finding]],
                run: Callable[
                    [Callable[[], list[Finding]]], Awaitable[list[Finding]]
                ] = original,
            ) -> list[Finding]:
                await asyncio.sleep(0.18)
                return await run(operation)

            monkeypatch.setattr(worker, "run", delayed_dispatch)
        response = client.post(
            "/v1/moderations", files={"file": ("photo.png", image_bytes)}
        )
        assert response.status_code == 503
        assert adapters["example/detector"].calls == 1
        assert adapters["example/classifier"].calls == 0
        assert "decision" not in response.json()


def test_cancellation_during_second_model_returns_all_workers_to_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    image_bytes: bytes,
) -> None:
    adapters = install(monkeypatch)
    settings = setup(tmp_path)
    release = Event()

    async def scenario() -> None:
        from modera.policies import load_policies

        policies = load_policies(settings.policy_file)
        service = engine.ModerationEngine(settings, policies)
        pending: asyncio.Task[ModerationResult] | None = None
        try:
            await service.start()
            adapters["example/classifier"].release = release
            pending = asyncio.create_task(
                service.evaluate(
                    "image", image_bytes, policies["default"], "req_" + "a" * 32
                )
            )
            async with asyncio.timeout(2):
                while not adapters["example/classifier"].calls:
                    await asyncio.sleep(0.005)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert service.worker_capacity("image").active == 1
            release.set()
            async with asyncio.timeout(2):
                while service.worker_capacity("image").active:
                    await asyncio.sleep(0.005)
            assert service.worker_capacity("image").waiting == 0
            result = await service.evaluate(
                "image", image_bytes, policies["default"], "req_" + "b" * 32
            )
            assert result.decision.action == "allow"
        finally:
            release.set()
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            service.close()

    asyncio.run(scenario())
