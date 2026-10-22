import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient
from starlette.datastructures import UploadFile

from modera import moderations
from modera.config import Settings
from modera.main import create_app
from modera.schemas import MAX_IMAGE_BYTES


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(Settings(docs_enabled=True))) as value:
        yield value


@pytest.mark.parametrize(
    "payload",
    [
        {"input": {"type": "text", "text": "hello"}},
        {
            "input": {
                "type": "image",
                "source": {"type": "url", "url": "https://media.example.com/photo.jpg"},
            },
            "policy": "community",
        },
    ],
)
def test_valid_json_has_no_moderation_verdict(client: TestClient, payload: object) -> None:
    response = client.post("/v1/moderations", json=payload)
    assert response.status_code == 501
    assert response.json() == {"detail": "moderation is not implemented"}


def test_invalid_json_fields_are_sanitized(client: TestClient) -> None:
    response = client.post(
        "/v1/moderations",
        json={"input": {"type": "text", "text": "sensitive content", "extra": "secret"}},
    )
    assert response.status_code == 422
    assert "sensitive content" not in response.text
    assert "secret" not in response.text
    assert response.json()["detail"][0]["loc"][0] == "body"


@pytest.mark.parametrize("body", [b"", b"{", b"null", b"[]"])
def test_malformed_json_is_rejected(client: TestClient, body: bytes) -> None:
    response = client.post(
        "/v1/moderations", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("policy", [None, "community"])
def test_file_upload_contract(client: TestClient, policy: str | None) -> None:
    response = client.post(
        "/v1/moderations",
        files={"file": ("photo.jpg", b"image bytes", "image/jpeg")},
        data={"policy": policy} if policy else {},
    )
    assert response.status_code == 501


def test_upload_is_closed_after_validation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[UploadFile] = []
    original = UploadFile.close

    async def close(file: UploadFile) -> None:
        await original(file)
        closed.append(file)

    monkeypatch.setattr(UploadFile, "close", close)
    response = client.post(
        "/v1/moderations",
        files={"file": ("photo.jpg", b"image bytes", "image/jpeg")},
        data={"policy": "invalid policy"},
    )
    assert response.status_code == 422
    assert len(closed) == 1
    assert closed[0].file.closed


@pytest.mark.parametrize("field", ["file", "unexpected"])
def test_empty_or_unknown_upload_is_rejected(client: TestClient, field: str) -> None:
    response = client.post("/v1/moderations", files={field: ("photo.jpg", b"", "image/jpeg")})
    assert response.status_code == 422


def test_multiple_files_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/moderations",
        files=[("file", ("a.jpg", b"a")), ("file", ("b.jpg", b"b"))],
    )
    assert response.status_code == 400


def test_unknown_form_field_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/moderations", files={"file": ("a.jpg", b"a")}, data={"text": "conflict"}
    )
    assert response.status_code == 422


def test_missing_multipart_boundary_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/moderations", content=b"invalid", headers={"content-type": "multipart/form-data"}
    )
    assert response.status_code == 400


def test_duplicate_file_and_text_field_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/moderations", files={"file": ("a.jpg", b"a")}, data={"file": "conflicting field"}
    )
    assert response.status_code == 422


def test_body_deadline_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(moderations, "BODY_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        async def body() -> AsyncIterator[bytes]:
            yield b'{"input":'
            await asyncio.sleep(0.1)
            yield b'{"type":"text","text":"hello"}}'

        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/v1/moderations", content=body(), headers={"content-type": "application/json"}
            )
            assert response.status_code == 408

    asyncio.run(scenario())


@pytest.mark.parametrize("size,status", [(MAX_IMAGE_BYTES, 501), (MAX_IMAGE_BYTES + 1, 413)])
def test_file_size_boundary(client: TestClient, size: int, status: int) -> None:
    response = client.post("/v1/moderations", files={"file": ("a.jpg", b"a" * size)})
    assert response.status_code == status


def test_streaming_body_limit_without_content_length(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(40):
            yield b"x" * 1024

    response = client.post(
        "/v1/moderations", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_unsupported_content_types(client: TestClient, content_type: str) -> None:
    response = client.post(
        "/v1/moderations", content=b"hello", headers={"content-type": content_type}
    )
    assert response.status_code == 415


def test_encoded_request_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/moderations",
        json={"input": {"type": "text", "text": "hello"}},
        headers={"content-encoding": "gzip"},
    )
    assert response.status_code == 415


def test_openapi_describes_both_encodings_and_resolves_references(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/moderations"]["post"]
    assert set(operation["requestBody"]["content"]) == {
        "application/json",
        "multipart/form-data",
    }
    assert "200" not in operation["responses"]
    models = schema["components"]["schemas"]
    assert (
        models["ModerationRequest"]["properties"]["input"]["discriminator"]["propertyName"]
        == "type"
    )
    assert models["ImageUploadRequest"]["properties"]["file"]["format"] == "binary"

    def check_refs(value: Any) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                target = schema
                for part in value["$ref"].removeprefix("#/").split("/"):
                    target = target[part]
            for nested in value.values():
                check_refs(nested)
        elif isinstance(value, list):
            for nested in value:
                check_refs(nested)

    check_refs(schema)
