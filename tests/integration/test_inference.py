import json
from pathlib import Path
from typing import TypedDict

import pytest
from fastapi.testclient import TestClient

from modera import engine
from modera.config import Settings
from modera.main import create_app
from modera.model_config import ImageModelSettings, TextModelSettings
from modera.results import Finding, ModerationResult

REVISION = "a" * 40


class FakeClassifier:
    fail = False

    def __init__(
        self, settings: TextModelSettings | ImageModelSettings, threads: int
    ) -> None:
        self.settings = settings

    def predict(self, content: str | bytes) -> list[Finding]:
        if self.fail:
            raise RuntimeError("private input must not leak")
        return [
            Finding(category=category, score=0.9 if content == "harmful" else 0.1)
            for category in self.settings.label_map.values()
        ]


class ConfigPaths(TypedDict):
    policy_file: Path
    text_rules_file: Path


def configuration(tmp_path: Path, *, model_required: bool = False) -> ConfigPaths:
    policy = tmp_path / "policy.json"
    text = [{"category": "rules.blocklist", "block_threshold": 1.0}]
    if model_required:
        text.append({"category": "toxicity", "block_threshold": 0.8})
    policy.write_text(
        json.dumps(
            {
                "policies": [
                    {
                        "id": "default",
                        "version": "1",
                        "text": text,
                        "image": [{"category": "sexual", "block_threshold": 0.8}],
                    }
                ]
            }
        )
    )
    rules = tmp_path / "rules.json"
    rules.write_text(
        '{"version":"rules-1","rules":[{"category":"rules.blocklist",'
        '"phrases":["blocked phrase"]}]}'
    )
    return {"policy_file": policy, "text_rules_file": rules}


def test_rules_only_returns_real_decision_and_provenance(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    with TestClient(
        create_app(Settings(**configuration(tmp_path))), headers=auth_headers
    ) as client:
        for content, action in [
            ("ordinary", "allow"),
            ("contains BLOCKED PHRASE", "block"),
        ]:
            response = client.post(
                "/v1/moderations", json={"input": {"type": "text", "text": content}}
            )
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            result = ModerationResult.model_validate(response.json())
            assert result.decision.action == action
            assert result.models == []
            assert result.rules_version == "rules-1"
            assert not result.fallback
            assert result.findings[0].source == "rules"
            assert result.request_id == response.headers["X-Request-ID"]


def test_configured_text_and_image_classifiers_return_shared_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    image_bytes: bytes,
) -> None:
    monkeypatch.setattr(engine, "Classifier", FakeClassifier)
    settings = Settings(
        **configuration(tmp_path, model_required=True),
        text_model=TextModelSettings(
            id="example/text", revision=REVISION, label_map={"native_toxic": "toxicity"}
        ),
        image_model=ImageModelSettings(
            id="example/image", revision=REVISION, label_map={"nsfw": "sexual"}
        ),
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        response = client.post(
            "/v1/moderations", json={"input": {"type": "text", "text": "harmful"}}
        )
        assert response.status_code == 200
        result = ModerationResult.model_validate(response.json())
        assert result.decision.action == "block"
        assert result.decision.reasons == ["toxicity"]
        assert result.models[0].revision == REVISION
        response = client.post(
            "/v1/moderations", files={"file": ("photo.png", image_bytes)}
        )
        assert response.status_code == 200
        assert response.json()["input_type"] == "image"
        assert response.json()["decision"]["action"] == "allow"


@pytest.mark.parametrize("required,expected", [(False, 200), (True, 503)])
def test_rules_fallback_does_not_satisfy_failed_model_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    required: bool,
    expected: int,
) -> None:
    monkeypatch.setattr(engine, "Classifier", FakeClassifier)
    settings = Settings(
        **configuration(tmp_path, model_required=required),
        text_model=TextModelSettings(
            id="example/model", revision=REVISION, label_map={"toxic": "toxicity"}
        ),
        text_fallback="rules",
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        monkeypatch.setattr(FakeClassifier, "fail", True)
        response = client.post(
            "/v1/moderations", json={"input": {"type": "text", "text": "ordinary"}}
        )
        assert response.status_code == expected
        if expected == 200:
            assert response.json()["fallback"]
            assert response.json()["models"] == []
            assert response.json()["coverage"]["evaluated"] == ["rules.blocklist"]
        else:
            assert response.json()["error"]["retryable"]
            assert "decision" not in response.json()
        assert "private input" not in response.text


def test_model_failure_has_no_automatic_rules_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_headers: dict[str, str]
) -> None:
    monkeypatch.setattr(engine, "Classifier", FakeClassifier)
    settings = Settings(
        **configuration(tmp_path),
        text_model=TextModelSettings(
            id="example/model", revision=REVISION, label_map={"toxic": "toxicity"}
        ),
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        monkeypatch.setattr(FakeClassifier, "fail", True)
        response = client.post(
            "/v1/moderations", json={"input": {"type": "text", "text": "hello"}}
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "check_unavailable"


def test_startup_failure_requires_explicit_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_headers: dict[str, str]
) -> None:
    def unavailable(
        settings: TextModelSettings | ImageModelSettings, threads: int
    ) -> FakeClassifier:
        raise RuntimeError("private model path")

    monkeypatch.setattr(engine, "Classifier", unavailable)
    values = configuration(tmp_path)
    model = TextModelSettings(
        id="example/model", revision=REVISION, label_map={"toxic": "toxicity"}
    )
    with (
        pytest.raises(RuntimeError, match="configured classifier"),
        TestClient(create_app(Settings(**values, text_model=model))),
    ):
        pass
    with TestClient(
        create_app(Settings(**values, text_model=model, text_fallback="rules")),
        headers=auth_headers,
    ) as client:
        response = client.post(
            "/v1/moderations", json={"input": {"type": "text", "text": "hello"}}
        )
        assert response.status_code == 200
        assert response.json()["fallback"]


def test_inference_requires_policy_and_fallback_requires_rules() -> None:
    with (
        pytest.raises(ValueError, match="rules fallback"),
        TestClient(create_app(Settings(text_fallback="rules"))),
    ):
        pass
    with (
        pytest.raises(ValueError, match="policy file"),
        TestClient(
            create_app(
                Settings(
                    text_model=TextModelSettings(id="example/model", revision=REVISION)
                )
            )
        ),
    ):
        pass


@pytest.mark.parametrize(
    "text,action", [("ordinary", "allow"), ("blocked phrase", "block")]
)
def test_explicit_degraded_policy_records_the_policy_actually_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    text: str,
    action: str,
) -> None:
    values = configuration(tmp_path, model_required=True)
    payload = json.loads(values["policy_file"].read_text())
    payload["policies"].append(
        {
            "id": "degraded",
            "version": "2",
            "text": [{"category": "rules.blocklist", "block_threshold": 1.0}],
        }
    )
    values["policy_file"].write_text(json.dumps(payload))
    monkeypatch.setattr(engine, "Classifier", FakeClassifier)
    settings = Settings(
        **values,
        text_model=TextModelSettings(
            id="example/model", revision=REVISION, label_map={"toxic": "toxicity"}
        ),
        text_fallback="rules",
        text_fallback_policy="degraded",
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        monkeypatch.setattr(FakeClassifier, "fail", True)
        response = client.post(
            "/v1/moderations", json={"input": {"type": "text", "text": text}}
        )
        assert response.status_code == 200
        result = response.json()
        assert result["fallback"]
        assert result["policy"] == {"id": "degraded", "version": "2"}
        assert result["decision"]["action"] == action
        assert result["coverage"]["evaluated"] == ["rules.blocklist"]
        assert result["models"] == []


def test_fallback_policy_cannot_claim_model_checks(tmp_path: Path) -> None:
    with (
        pytest.raises(ValueError, match="only configured text rules"),
        TestClient(
            create_app(
                Settings(
                    **configuration(tmp_path, model_required=True),
                    text_fallback="rules",
                    text_fallback_policy="default",
                )
            )
        ),
    ):
        pass
