import logging
from collections.abc import Mapping
from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    param: str | None = None
    retryable: bool = False
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class APIError(HTTPException):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        param: str | None = None,
        retryable: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(status_code, detail=message, headers=headers)
        self.code = code
        self.message = message
        self.param = param
        self.retryable = retryable


def error_response(error: APIError, request_id: str) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=error.code,
            message=error.message,
            param=error.param,
            retryable=error.retryable,
            request_id=request_id,
        )
    )
    headers = dict(error.headers or {})
    headers["Cache-Control"] = "no-store"
    return JSONResponse(body.model_dump(), status_code=error.status_code, headers=headers)


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        raise TypeError("expected an HTTP exception")
    if isinstance(exc, APIError):
        error = exc
    else:
        code, message = {
            400: ("invalid_request", "The request is invalid."),
            401: ("authentication_required", "Valid authentication credentials are required."),
            403: ("forbidden", "Access to this resource is forbidden."),
            404: ("not_found", "The requested resource was not found."),
            405: ("method_not_allowed", "The HTTP method is not allowed for this resource."),
        }.get(exc.status_code, ("http_error", "The request could not be completed."))
        error = APIError(exc.status_code, code, message, headers=exc.headers)
    return error_response(error, request.state.request_id)


def validation_error(exc: ValidationError | RequestValidationError) -> APIError:
    first = exc.errors()[0]
    if first["type"] == "json_invalid":
        return APIError(400, "invalid_json", "The request body is not valid JSON.")

    location = [part for part in first["loc"] if isinstance(part, str)]
    if location and location[0] == "body":
        location.pop(0)
    if len(location) > 2 and location[:2] in (["input", "text"], ["input", "image"]):
        location.pop(1)
    if first["type"] in {"union_tag_invalid", "union_tag_not_found"}:
        location.append("type")

    fields = {
        "input",
        "input.type",
        "input.text",
        "input.source",
        "input.source.type",
        "input.source.url",
        "policy",
        "file",
    }
    while location and ".".join(location) not in fields:
        location.pop()
    param = ".".join(location) or None
    messages = {
        "missing": "A required field is missing.",
        "extra_forbidden": "Unknown fields are not allowed.",
        "string_type": "The field must be a string.",
        "string_too_short": "The value is too short.",
        "string_too_long": "The value exceeds its length limit.",
        "string_pattern_mismatch": "The value does not match the required format.",
        "union_tag_not_found": "The input.type field is required.",
        "model_type": "A JSON object is required.",
    }
    message = messages.get(first["type"], "Request validation failed.")
    if first["type"] == "union_tag_invalid":
        return APIError(
            400, "unsupported_input_type", "Supported input types are text and image.", param=param
        )
    if param == "input.source.url":
        message = "A valid HTTPS URL without credentials or fragments is required."
    elif first["type"] == "value_error" and param == "input.text":
        message = "Text must not be blank."
    return APIError(400, "invalid_request", message, param=param)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise TypeError("expected a request validation exception")
    return error_response(validation_error(exc), request.state.request_id)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = f"req_{uuid4().hex}"
        scope.setdefault("state", {})["request_id"] = request_id
        started = False

        async def send_with_id(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            if started:
                raise
            logger.error(
                "Unhandled request error: request_id=%s type=%s", request_id, type(exc).__name__
            )
            response = error_response(
                APIError(500, "internal_error", "An unexpected error occurred."), request_id
            )
            await response(scope, receive, send_with_id)
