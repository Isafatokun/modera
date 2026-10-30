import logging
from collections.abc import Iterator
from secrets import compare_digest

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


def test_rotation_accepts_overlap_then_rejects_retired_key(
    auth_headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    old = auth_headers["Authorization"].removeprefix("Bearer ")
    new = "new-test-key-for-overlapping-key-rotation"
    for previous in (SecretStr(old), None):
        settings = Settings(api_key=SecretStr(new), previous_api_key=previous)
        assert old not in repr(settings) + settings.model_dump_json()
        assert new not in repr(settings) + settings.model_dump_json()
        with TestClient(create_app(settings)) as client:
            for key, status in ((new, 200), (old, 200 if previous else 401)):
                response = client.get(
                    "/metrics", headers={"Authorization": f"Bearer {key}"}
                )
                assert response.status_code == status
                assert key not in response.text + caplog.text
            assert client.get("/health/live").status_code == 200


@pytest.mark.parametrize("key", ["short-private-key", "é" * 32, " " * 32])
def test_previous_key_has_same_validation_and_redaction(key: str) -> None:
    with pytest.raises(ValidationError) as failure:
        Settings(previous_api_key=SecretStr(key))
    assert key not in str(failure.value)


def test_duplicate_rotation_keys_are_rejected(auth_headers: dict[str, str]) -> None:
    key = auth_headers["Authorization"].removeprefix("Bearer ")
    with pytest.raises(ValidationError):
        Settings(previous_api_key=SecretStr(key))


def test_rotation_compares_both_keys_even_when_current_matches(
    monkeypatch: pytest.MonkeyPatch, auth_headers: dict[str, str]
) -> None:
    from modera import auth

    original = compare_digest
    comparisons = 0

    def compare(first: bytes, second: bytes) -> bool:
        nonlocal comparisons
        comparisons += 1
        return original(first, second)

    monkeypatch.setattr(auth, "compare_digest", compare)
    with TestClient(
        create_app(
            Settings(
                previous_api_key=SecretStr("previous-test-key-for-bounded-key-rotation")
            )
        )
    ) as client:
        assert client.get("/metrics", headers=auth_headers).status_code == 200
        assert comparisons == 2
        assert (
            client.get(
                "/metrics",
                headers=[
                    ("Authorization", auth_headers["Authorization"]),
                    ("Authorization", auth_headers["Authorization"]),
                ],
            ).status_code
            == 401
        )
        assert comparisons == 2
