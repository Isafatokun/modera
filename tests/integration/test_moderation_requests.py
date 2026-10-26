import asyncio
from collections.abc import AsyncIterator, Iterator
from tempfile import SpooledTemporaryFile
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient
from starlette import formparsers
from starlette.datastructures import UploadFile

from modera import moderations
from modera.config import Settings
from modera.main import create_app
from modera.schemas import MAX_IMAGE_BYTES


@pytest.fixture
def client(auth_headers: dict[str, str]) -> Iterator[TestClient]:
    with TestClient(
        create_app(Settings(docs_enabled=True)), headers=auth_headers
    ) as value:
        yield value


@pytest.mark.parametrize(
    "payload",
    [
        {"input": {"type": "text", "text": "hello"}},
    ],
)
def test_valid_json_has_no_moderation_verdict(
    client: TestClient, payload: object
) -> None:
    response = client.post("/v1/moderations", json=payload)
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "moderation_not_implemented"
    assert response.json()["error"]["retryable"] is False
    assert "decision" not in response.json()


def test_invalid_json_fields_are_sanitized(client: TestClient) -> None:
    response = client.post(
        "/v1/moderations",
        json={
            "input": {"type": "text", "text": "sensitive content", "extra": "secret"}
        },
    )
    assert response.status_code == 400
    assert "sensitive content" not in response.text
    assert "secret" not in response.text
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["param"] == "input"


@pytest.mark.parametrize("body", [b"", b"{", b"null", b"[]"])
def test_malformed_json_is_rejected(client: TestClient, body: bytes) -> None:
    response = client.post(
        "/v1/moderations", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize("policy", [None, "community"])
def test_file_upload_contract(
    client: TestClient, policy: str | None, image_bytes: bytes
) -> None:
    response = client.post(
        "/v1/moderations",
        files={"file": ("photo.jpg", image_bytes, "image/jpeg")},
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
    assert response.status_code == 400
    assert len(closed) == 1
    assert closed[0].file.closed


@pytest.mark.parametrize("field", ["file", "unexpected"])
def test_empty_or_unknown_upload_is_rejected(client: TestClient, field: str) -> None:
    response = client.post(
        "/v1/moderations", files={field: ("photo.jpg", b"", "image/jpeg")}
    )
    assert response.status_code == 400


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
    assert response.status_code == 400


def test_missing_multipart_boundary_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/moderations",
        content=b"invalid",
        headers={"content-type": "multipart/form-data"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("ending", [b"", b"--b", b"--b\r\n"])
def test_truncated_policy_does_not_silently_use_default(
    client: TestClient, ending: bytes
) -> None:
    body = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\nimage bytes\r\n"
        b'--b\r\nContent-Disposition: form-data; name="policy"\r\n\r\ncommunity\r\n'
        + ending
    )
    response = client.post(
        "/v1/moderations",
        content=body,
        headers={"content-type": "multipart/form-data; boundary=b"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_multipart"


@pytest.mark.parametrize("ending", [b"", b"\r\n--b\r\n"])
def test_truncated_upload_is_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, ending: bytes
) -> None:
    files: list[SpooledTemporaryFile[bytes]] = []

    def create_file(max_size: int) -> SpooledTemporaryFile[bytes]:
        file = SpooledTemporaryFile(max_size=max_size)  # noqa: SIM115 — parser owns file lifetime
        files.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", create_file)
    body = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.jpg"\r\n'
        b"\r\nimage bytes" + ending
    )
    response = client.post(
        "/v1/moderations",
        content=body,
        headers={"content-type": "multipart/form-data; boundary=b"},
    )
    assert response.status_code == 400
    assert len(files) == 1
    assert files[0].closed


def test_duplicate_file_and_text_field_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/moderations",
        files={"file": ("a.jpg", b"a")},
        data={"file": "conflicting field"},
    )
    assert response.status_code == 400


def test_body_deadline_is_enforced(
    monkeypatch: pytest.MonkeyPatch, auth_headers: dict[str, str]
) -> None:
    monkeypatch.setattr(moderations, "BODY_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        async def body() -> AsyncIterator[bytes]:
            yield b'{"input":'
            await asyncio.sleep(0.1)
            yield b'{"type":"text","text":"hello"}}'

        async with AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://testserver",
            headers=auth_headers,
        ) as client:
            response = await client.post(
                "/v1/moderations",
                content=body(),
                headers={"content-type": "application/json"},
            )
            assert response.status_code == 408

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "size,status", [(MAX_IMAGE_BYTES, 501), (MAX_IMAGE_BYTES + 1, 413)]
)
def test_file_size_boundary(
    client: TestClient, size: int, status: int, image_bytes: bytes
) -> None:
    data = image_bytes + b"\0" * (size - len(image_bytes))
    response = client.post("/v1/moderations", files={"file": ("a.jpg", data)})
    assert response.status_code == status


def test_streaming_body_limit_without_content_length(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(40):
            yield b"x" * 1024

    response = client.post(
        "/v1/moderations",
        content=chunks(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "content_type", ["text/plain", "application/x-www-form-urlencoded", ""]
)
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


def test_openapi_describes_both_encodings_and_resolves_references(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/moderations"]["post"]
    assert set(operation["requestBody"]["content"]) == {
        "application/json",
        "multipart/form-data",
    }
    assert "200" in operation["responses"]
    assert "400" in operation["responses"]
    assert "422" in operation["responses"]
    models = schema["components"]["schemas"]
    assert "ModerationResult" in models
    assert models["Decision"]["properties"]["action"]["enum"] == [
        "allow",
        "review",
        "block",
    ]
    assert (
        models["ModerationRequest"]["properties"]["input"]["discriminator"][
            "propertyName"
        ]
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


@pytest.mark.parametrize("failure", ["limit", "timeout", "cancel"])
def test_interrupted_uploads_close_temporary_files(
    monkeypatch: pytest.MonkeyPatch, auth_headers: dict[str, str], failure: str
) -> None:
    files: list[SpooledTemporaryFile[bytes]] = []
    created = asyncio.Event()

    def create_file(max_size: int) -> SpooledTemporaryFile[bytes]:
        file = SpooledTemporaryFile(max_size=1)  # noqa: SIM115
        files.append(file)
        created.set()
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", create_file)
    monkeypatch.setattr(moderations, "MAX_MULTIPART_BYTES", 2048)
    if failure == "timeout":
        monkeypatch.setattr(moderations, "BODY_TIMEOUT_SECONDS", 0.02)

    async def scenario() -> None:
        async def body() -> AsyncIterator[bytes]:
            yield (
                b'--b\r\nContent-Disposition: form-data; name="file"; '
                b'filename="a.png"\r\n\r\n' + b"x" * 1024
            )
            if failure == "limit":
                yield b"x" * 2048
            else:
                await asyncio.sleep(1)
            yield b"\r\n--b--\r\n"

        app = create_app()
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
                headers=auth_headers,
            ) as client,
        ):
            task = asyncio.create_task(
                client.post(
                    "/v1/moderations",
                    content=body(),
                    headers={"Content-Type": "multipart/form-data; boundary=b"},
                )
            )
            if failure == "cancel":
                await asyncio.wait_for(created.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                response = await task
                assert response.status_code == (413 if failure == "limit" else 408)
        assert len(files) == 1
        assert files[0].closed

    asyncio.run(scenario())
