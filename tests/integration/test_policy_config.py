import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modera.config import Settings
from modera.main import create_app


def write_policy(path: Path, version: str = "1") -> None:
    path.write_text(
        json.dumps(
            {
                "policies": [
                    {
                        "id": "default",
                        "version": version,
                        "text": [{"category": "violence", "block_threshold": 0.85}],
                    }
                ]
            }
        )
    )


def test_configured_policy_selection_and_shared_errors(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    path = tmp_path / "policies.json"
    write_policy(path)
    with TestClient(
        create_app(Settings(policy_file=path)), headers=auth_headers
    ) as client:
        payload = {"input": {"type": "text", "text": "hello"}}
        assert client.post("/v1/moderations", json=payload).status_code == 501
        response = client.post("/v1/moderations", json={**payload, "policy": "unknown"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_policy"
        assert response.json()["error"]["param"] == "policy"
        assert response.json()["error"]["retryable"] is False
        response = client.post(
            "/v1/moderations", files={"file": ("photo.jpg", b"image bytes")}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "policy_not_applicable"
        response = client.post(
            "/v1/moderations",
            files={"file": ("photo.jpg", b"image bytes")},
            data={"policy": "unknown"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_policy"


def test_invalid_config_prevents_app_creation(tmp_path: Path) -> None:
    path = tmp_path / "policies.json"
    path.write_text('{"policies":[]}')
    with pytest.raises(ValueError):
        create_app(Settings(policy_file=path))


def test_applications_keep_independent_policy_snapshots(tmp_path: Path) -> None:
    path = tmp_path / "policies.json"
    write_policy(path)
    first = create_app(Settings(policy_file=path))
    write_policy(path, "2")
    second = create_app(Settings(policy_file=path))
    assert first.state.policies["default"].version == "1"
    assert second.state.policies["default"].version == "2"


def test_policy_file_can_be_set_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "policies.json"
    write_policy(path)
    monkeypatch.setenv("MODERA_POLICY_FILE", str(path))
    assert create_app().state.policies["default"].version == "1"
