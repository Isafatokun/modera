import logging
import re

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from modera.config import Settings
from modera.errors import ErrorResponse
from modera.main import create_app


@pytest.mark.parametrize(
    "payload,code,param",
    [
        ({"input": {"type": "text", "text": " "}}, "invalid_request", "input.text"),
        ({"input": {"type": "text"}}, "invalid_request", "input.text"),
        ({"input": {"type": "audio-secret"}}, "unsupported_input_type", "input.type"),
        ({"input": {"text": "hello"}}, "invalid_request", "input.type"),
        (
            {"input": {"type": "image", "source": {"type": "url", "url": "secret-value"}}},
            "invalid_request",
            "input.source.url",
        ),
        (
            {"input": {"type": "text", "text": "hello", "secret-field": "secret-value"}},
            "invalid_request",
            "input",
        ),
        (
            {"input": {"type": "text", "text": "hello"}, "policy": "secret value"},
            "invalid_request",
            "policy",
        ),
    ],
)
def test_validation_error_contract(
    payload: object, code: str, param: str, auth_headers: dict[str, str]
) -> None:
    with TestClient(create_app(), headers=auth_headers) as client:
        response = client.post("/v1/moderations", json=payload)
    assert response.status_code == 400
    error = ErrorResponse.model_validate(response.json()).error
    assert error.code == code
    assert error.param == param
    assert error.retryable is False
    assert error.request_id == response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "secret" not in response.text


def test_malformed_json_has_no_field_or_submitted_content(auth_headers: dict[str, str]) -> None:
    with TestClient(create_app(), headers=auth_headers) as client:
        response = client.post(
            "/v1/moderations", content=b'{"secret":', headers={"content-type": "application/json"}
        )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_json"
    assert error["param"] is None
    assert "secret" not in response.text


def test_request_ids_are_generated_for_errors_and_successes() -> None:
    with TestClient(create_app()) as client:
        success = client.get("/health/live", headers={"X-Request-ID": "untrusted"})
        failure = client.get("/missing")
    first, second = success.headers["X-Request-ID"], failure.headers["X-Request-ID"]
    assert success.status_code == 200
    assert failure.status_code == 404
    assert re.fullmatch(r"req_[0-9a-f]{32}", first)
    assert first != second
    assert failure.json()["error"]["request_id"] == second
    assert failure.json()["error"]["code"] == "not_found"


def test_method_not_allowed_preserves_allow_header() -> None:
    with TestClient(create_app()) as client:
        response = client.post("/health/live")
    assert response.status_code == 405
    assert "GET" in response.headers["Allow"]
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_readiness_explains_retry_without_moderation_decision() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.ready = False
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["code"] == "service_not_ready"
    assert response.json()["error"]["retryable"] is True


def test_unexpected_errors_are_sanitized_in_response_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app()

    @app.get("/broken")
    async def broken() -> None:
        raise RuntimeError("sensitive-content token=secret-value")

    with caplog.at_level(logging.ERROR, logger="modera.errors"), TestClient(app) as client:
        response = client.get("/broken")
        assert client.get("/health/live").status_code == 200
    assert response.status_code == 500
    error = ErrorResponse.model_validate(response.json()).error
    assert error.code == "internal_error"
    assert error.retryable is False
    assert error.request_id == response.headers["X-Request-ID"]
    assert error.request_id in caplog.text
    assert "RuntimeError" in caplog.text
    for value in ("sensitive-content", "secret-value", "Traceback"):
        assert value not in response.text
        assert value not in caplog.text


def test_framework_validation_uses_the_same_contract() -> None:
    app = create_app()

    @app.get("/query")
    async def query(count: int) -> int:
        return count

    with TestClient(app) as client:
        response = client.get("/query", params={"count": "secret-value"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "secret-value" not in response.text


def test_generic_http_error_does_not_echo_exception_details() -> None:
    app = create_app()

    @app.get("/forbidden")
    async def forbidden() -> None:
        raise HTTPException(403, detail="sensitive-content")

    with TestClient(app) as client:
        response = client.get("/forbidden")
    assert response.status_code == 403
    assert "sensitive-content" not in response.text


def test_openapi_uses_the_shared_error_schema() -> None:
    with TestClient(create_app(Settings(docs_enabled=True))) as client:
        schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/v1/moderations"]["post"]["responses"]
    for status in ("400", "408", "413", "415", "500", "501"):
        assert responses[status]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
