from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from modera.config import Settings
from modera.downloads import ImageDownloader, source_error
from modera.main import create_app

PAYLOAD = {
    "input": {
        "type": "image",
        "source": {"type": "url", "url": "https://media.example.com/a?secret=value"},
    }
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
        (None, 501, "moderation_not_implemented"),
        (b"bad", 415, "unsupported_image_format"),
        (b"\xff\xd8\xffbad", 422, "invalid_image"),
    ],
)
def test_url_bytes_pass_through_image_validator(
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    image_bytes: bytes,
    content: bytes | None,
    status: int,
    code: str,
) -> None:
    method = AsyncMock(return_value=image_bytes if content is None else content)
    monkeypatch.setattr(ImageDownloader, "_download", method)
    with TestClient(
        create_app(Settings(image_allowed_hosts=("media.example.com",))),
        headers=auth_headers,
    ) as client:
        response = client.post("/v1/moderations", json=PAYLOAD)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    if status != 501:
        assert response.json()["error"]["param"] == "input.source.url"
    assert method.await_count == 1


def test_failed_source_returns_retryable_error_without_decision(
    monkeypatch: pytest.MonkeyPatch, auth_headers: dict[str, str]
) -> None:
    method = AsyncMock(
        side_effect=source_error(
            503, "input_fetch_failed", "Unavailable", retryable=True
        )
    )
    monkeypatch.setattr(ImageDownloader, "_download", method)
    with TestClient(
        create_app(Settings(image_allowed_hosts=("media.example.com",))),
        headers=auth_headers,
    ) as client:
        response = client.post("/v1/moderations", json=PAYLOAD)
        assert client.get("/health/ready").status_code == 200
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["retryable"]
    assert "decision" not in response.json()
    assert method.await_count == 2
