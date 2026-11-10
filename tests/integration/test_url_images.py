import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from modera import engine
from modera.config import Settings
from modera.downloads import ImageDownloader, source_error
from modera.main import create_app
from modera.model_config import ImageModelSettings
from modera.results import Finding

PAYLOAD = {
    "input": {
        "type": "image",
        "source": {"type": "url", "url": "https://media.example.com/a?secret=value"},
    }
}


class FakeImageClassifier:
    def __init__(self, settings: Any, threads: int) -> None:
        self.settings = settings

    def predict(self, content: Any) -> list[Finding]:
        return [Finding(category="sexual", score=0.1)]


@pytest.fixture
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "policies": [
                    {
                        "id": "default",
                        "version": "1",
                        "image": [{"category": "sexual", "block_threshold": 0.8}],
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(engine, "create_adapter", FakeImageClassifier)
    return {
        "policy_file": policy,
        "image_model": ImageModelSettings(
            id="example/image", revision="a" * 40, label_map={"nsfw": "sexual"}
        ),
        "image_allowed_hosts": ("media.example.com",),
    }


def test_url_downloads_disabled_by_default(auth_headers: dict[str, str]) -> None:
    with TestClient(create_app(), headers=auth_headers) as client:
        response = client.post("/v1/moderations", json=PAYLOAD)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "image_url_not_allowed"
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "content,status,code",
    [
        (None, 200, None),
        (b"bad", 415, "unsupported_image_format"),
        (b"\xff\xd8\xffbad", 422, "invalid_image"),
    ],
)
def test_url_bytes_pass_through_image_validator(
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    backend: dict[str, Any],
    image_bytes: bytes,
    content: bytes | None,
    status: int,
    code: str | None,
) -> None:
    method = AsyncMock(return_value=image_bytes if content is None else content)
    monkeypatch.setattr(ImageDownloader, "_download", method)
    with TestClient(create_app(Settings(**backend)), headers=auth_headers) as client:
        response = client.post("/v1/moderations", json=PAYLOAD)
    assert response.status_code == status
    if code is not None:
        assert response.json()["error"]["code"] == code
        assert response.json()["error"]["param"] == "input.source.url"
    assert method.await_count == 1


def test_url_is_not_fetched_when_no_image_backend_is_configured(
    monkeypatch: pytest.MonkeyPatch, auth_headers: dict[str, str]
) -> None:
    method = AsyncMock(return_value=b"unused")
    monkeypatch.setattr(ImageDownloader, "_download", method)
    with TestClient(
        create_app(Settings(image_allowed_hosts=("media.example.com",))),
        headers=auth_headers,
    ) as client:
        response = client.post("/v1/moderations", json=PAYLOAD)
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "moderation_not_implemented"
    assert method.await_count == 0


def test_failed_source_returns_retryable_error_without_decision(
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    backend: dict[str, Any],
) -> None:
    method = AsyncMock(
        side_effect=source_error(
            503, "input_fetch_failed", "Unavailable", retryable=True
        )
    )
    monkeypatch.setattr(ImageDownloader, "_download", method)
    with TestClient(create_app(Settings(**backend)), headers=auth_headers) as client:
        response = client.post("/v1/moderations", json=PAYLOAD)
        assert client.get("/health/ready").status_code == 200
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["retryable"]
    assert "decision" not in response.json()
    assert method.await_count == 2
