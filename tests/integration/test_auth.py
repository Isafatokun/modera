import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from modera.config import Settings
from modera.main import create_app


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Basic invalid", "Bearer wrong-key", "Bearer wrong key"],
)
def test_missing_or_invalid_credentials_are_rejected(
    authorization: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    with caplog.at_level(logging.DEBUG), TestClient(create_app()) as client:
        response = client.post(
            "/v1/moderations", headers=headers, content=b"invalid body"
        )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    error = response.json()["error"]
    assert error["code"] == "authentication_required"
    assert error["retryable"] is False
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert "wrong-key" not in response.text + caplog.text


def test_valid_key_authorizes_json_and_file_upload(
    auth_headers: dict[str, str], image_bytes: bytes
) -> None:
    with TestClient(create_app(), headers=auth_headers) as client:
        response = client.post(
            "/v1/moderations", json={"input": {"type": "text", "text": "hello"}}
        )
        assert response.status_code == 501
        response = client.post(
            "/v1/moderations", files={"file": ("photo.jpg", image_bytes)}
        )
        assert response.status_code == 501


def test_authentication_happens_before_body_reading() -> None:
    consumed = False

    def body() -> Iterator[bytes]:
        nonlocal consumed
        consumed = True
        yield b"invalid body"

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/moderations",
            content=body(),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 401
    assert not consumed


def test_duplicate_authorization_headers_are_rejected(
    auth_headers: dict[str, str],
) -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/moderations",
            headers=[("Authorization", auth_headers["Authorization"])] * 2,
            json={"input": {"type": "text", "text": "hello"}},
        )
    assert response.status_code == 401


def test_non_ascii_credentials_are_rejected_without_server_error() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/moderations", headers={b"Authorization": b"Bearer wrong-\xe9"}
        )
    assert response.status_code == 401


def test_scheme_is_case_insensitive_but_key_is_case_sensitive(
    auth_headers: dict[str, str],
) -> None:
    key = auth_headers["Authorization"].removeprefix("Bearer ")
    payload = {"input": {"type": "text", "text": "hello"}}
    with TestClient(create_app()) as client:
        assert (
            client.post(
                "/v1/moderations",
                json=payload,
                headers={"Authorization": f"bearer {key}"},
            ).status_code
            == 501
        )
        assert (
            client.post(
                "/v1/moderations",
                json=payload,
                headers={"Authorization": f"Bearer {key.upper()}"},
            ).status_code
            == 401
        )


def test_health_checks_remain_unauthenticated() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200


def test_missing_key_prevents_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODERA_API_KEY")
    with pytest.raises(ValidationError):
        create_app()


@pytest.mark.parametrize(
    "key", ["", "short-secret", " " * 32, "x" * 513, "x" * 31 + "\n", "é" * 32]
)
def test_invalid_keys_prevent_startup_without_leaking_values(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv("MODERA_API_KEY", key)
    with pytest.raises(ValidationError) as failure:
        create_app()
    assert "input_value" not in str(failure.value)
    assert "short-secret" not in str(failure.value)


def test_settings_and_responses_do_not_expose_the_key(
    auth_headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    key = auth_headers["Authorization"].removeprefix("Bearer ")
    settings = Settings()
    assert key not in repr(settings)
    assert key not in settings.model_dump_json()
    with (
        caplog.at_level(logging.DEBUG),
        TestClient(create_app(settings), headers=auth_headers) as client,
    ):
        response = client.post(
            "/v1/moderations", json={"input": {"type": "text", "text": "hello"}}
        )
    assert key not in response.text
    assert key not in caplog.text


def test_applications_do_not_share_authentication_keys(
    auth_headers: dict[str, str],
) -> None:
    with (
        TestClient(create_app(), headers=auth_headers) as first,
        TestClient(
            create_app(
                Settings(api_key=SecretStr("another-test-key-for-an-isolated-service"))
            )
        ) as second,
    ):
        response = second.post(
            "/v1/moderations",
            headers=auth_headers,
            json={"input": {"type": "text", "text": "hello"}},
        )
        assert response.status_code == 401
        assert (
            first.post(
                "/v1/moderations", json={"input": {"type": "text", "text": "hello"}}
            ).status_code
            == 501
        )


def test_openapi_documents_bearer_authentication() -> None:
    with TestClient(create_app(Settings(docs_enabled=True))) as client:
        schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/moderations"]["post"]
    assert operation["security"] == [{"ApiKey": []}]
    assert "401" in operation["responses"]
    assert schema["components"]["securitySchemes"]["ApiKey"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "Shared service API key",
    }
    assert "security" not in schema["paths"]["/health/live"]["get"]
