import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from enum import StrEnum

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from pydantic import HttpUrl, ValidationError
from pydantic.json_schema import models_json_schema
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser

from modera.admission import admit_request
from modera.auth import authenticate
from modera.engine import ModerationEngine
from modera.errors import APIError, ErrorResponse, validation_error
from modera.inference import PreparedImage
from modera.input_types import IMAGE, TEXT, InputType
from modera.policies import resolve_policy
from modera.results import ModerationResult
from modera.schemas import (
    MAX_IMAGE_BYTES,
    ImageUploadRequest,
    ModerationRequest,
    TextInput,
)

MAX_JSON_BYTES = 32 * 1024
MAX_MULTIPART_BYTES = 9 * 1024 * 1024
BODY_TIMEOUT_SECONDS = 15

router = APIRouter(
    prefix="/v1",
    tags=["moderations"],
    dependencies=[Depends(authenticate), Depends(admit_request)],
)


class InputSource(StrEnum):
    """Internal transport discriminator; never used as an API input type."""

    TEXT = "text"
    IMAGE_URL = "image_url"
    IMAGE_UPLOAD = "image_upload"


@dataclass(frozen=True, slots=True)
class ResolvedInput:
    policy: str
    source: InputSource
    content: str | bytes

    @property
    def input_type(self) -> InputType:
        return TEXT if self.source is InputSource.TEXT else IMAGE


class CompleteMultipartParser(MultiPartParser):
    complete = False

    def on_end(self) -> None:
        self.complete = True

    async def parse(self) -> FormData:
        stream = self.stream

        async def checked_stream() -> AsyncGenerator[bytes]:
            async for chunk in stream:
                yield chunk
            if not self.complete:
                raise MultiPartException("incomplete multipart body")

        self.stream = checked_stream()
        return await super().parse()


async def limited_stream(request: Request, limit: int) -> AsyncGenerator[bytes]:
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise APIError(413, "payload_too_large", "The request body is too large.")
        yield chunk


async def validate_json(request: Request) -> ModerationRequest:
    body = bytearray()
    async for chunk in limited_stream(request, MAX_JSON_BYTES):
        body.extend(chunk)
    return ModerationRequest.model_validate_json(body)


async def validate_upload(request: Request) -> tuple[str, bytes]:
    parser = CompleteMultipartParser(
        request.headers,
        limited_stream(request, MAX_MULTIPART_BYTES),
        max_files=1,
        max_fields=1,
        max_part_size=1024,
    )
    try:
        form = await parser.parse()
    except (MultiPartException, MultipartParseError) as exc:
        raise APIError(
            400, "invalid_multipart", "The multipart body is invalid or incomplete."
        ) from exc
    try:
        if len(form.multi_items()) != len(form):
            raise APIError(
                400, "invalid_request", "Duplicate form fields are not allowed."
            )
        upload = ImageUploadRequest.model_validate(dict(form))
        if not upload.file.size:
            raise APIError(
                400,
                "invalid_request",
                "The image file must not be empty.",
                param="file",
            )
        if upload.file.size > MAX_IMAGE_BYTES:
            raise APIError(
                413, "payload_too_large", "The image file is too large.", param="file"
            )
        return upload.policy, await upload.file.read(MAX_IMAGE_BYTES + 1)
    finally:
        await form.close()


@router.post(
    "/moderations",
    status_code=200,
    response_model=ModerationResult,
    summary="Moderate text or an image",
    description=("Evaluate text or an image using the configured backends and policy."),
    responses={
        status: {"model": ErrorResponse, "description": description}
        for status, description in {
            400: "Malformed body or invalid request fields",
            401: "Missing or invalid bearer API key",
            408: "Request body deadline exceeded",
            413: "Request or image file exceeds its size limit",
            415: "Unsupported request content type or content encoding",
            422: "Image data is invalid or the source rejected retrieval",
            503: "Image processing is busy or the source is temporarily unavailable",
            500: "Internal model or result failure; no decision",
            501: "No moderation backend is configured for this input",
        }.items()
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ModerationRequest"}
                },
                "multipart/form-data": {
                    "schema": {"$ref": "#/components/schemas/ImageUploadRequest"}
                },
            },
        }
    },
)
async def moderate(request: Request, response: Response) -> ModerationResult:
    response.headers["Cache-Control"] = "no-store"
    resolved: ResolvedInput
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise APIError(
            415,
            "unsupported_content_encoding",
            "Content encoding is not supported.",
            headers={"Accept-Encoding": "identity"},
        )
    media_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    try:
        async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
            match media_type:
                case "application/json":
                    payload = await validate_json(request)
                    if isinstance(payload.input, TextInput):
                        resolved = ResolvedInput(
                            payload.policy, InputSource.TEXT, payload.input.text
                        )
                    else:
                        resolved = ResolvedInput(
                            payload.policy,
                            InputSource.IMAGE_URL,
                            str(payload.input.source.url),
                        )
                case "multipart/form-data":
                    policy_name, image_data = await validate_upload(request)
                    resolved = ResolvedInput(
                        policy_name, InputSource.IMAGE_UPLOAD, image_data
                    )
                case _:
                    raise APIError(
                        415,
                        "unsupported_media_type",
                        "The request content type is not supported.",
                        headers={"Accept": "application/json, multipart/form-data"},
                    )
    except TimeoutError as exc:
        raise APIError(
            408,
            "request_timeout",
            "The request body deadline was exceeded.",
            retryable=True,
        ) from exc
    except ValidationError as exc:
        raise validation_error(exc) from exc
    policy_name = resolved.policy
    input_type = resolved.input_type
    content: str | bytes | PreparedImage = resolved.content
    if request.app.state.policies:
        resolve_policy(request.app.state.policies, policy_name, input_type)
    is_url = resolved.source is InputSource.IMAGE_URL
    engine: ModerationEngine | None = getattr(request.app.state, "engine", None)
    if is_url:
        downloader = request.app.state.downloads
        if downloader is None:
            raise APIError(
                503, "service_not_ready", "The service is not ready.", retryable=True
            )
        if not isinstance(resolved.content, str):
            raise TypeError("URL input must contain a URL string")
        image_data = await downloader.fetch(HttpUrl(resolved.content))
    elif resolved.source is InputSource.IMAGE_UPLOAD:
        if not isinstance(resolved.content, bytes):
            raise TypeError("uploaded input must contain bytes")
        image_data = resolved.content
    if resolved.source is not InputSource.TEXT:
        validator = request.app.state.images
        if validator is None:
            raise APIError(
                503,
                "service_not_ready",
                "The service is not ready.",
                retryable=True,
                headers={"Retry-After": "1"},
            )
        try:
            preprocessor = engine.image_preprocessor if engine is not None else None
            if preprocessor is None:
                await validator.validate(image_data)
                content = image_data
            else:
                content = await validator.prepare(image_data, preprocessor)
        except APIError as exc:
            if is_url and exc.param == "file":
                exc.param = "input.source.url"
            raise
        del image_data
    if engine is None or not request.app.state.ready:
        raise APIError(
            503,
            "service_not_ready",
            "The service is not ready.",
            retryable=True,
            headers={"Retry-After": "1"},
        )
    if not engine.enabled(input_type):
        raise APIError(
            501,
            "moderation_not_implemented",
            "No moderation backend is configured for this input.",
        )
    policy = resolve_policy(request.app.state.policies, policy_name, input_type)
    result = await engine.evaluate(
        input_type, content, policy, request.state.request_id
    )
    request.app.state.metrics.observe_decision(result)
    return result


def register_contract_schemas(app: FastAPI) -> None:
    _, definitions = models_json_schema(
        [
            (ModerationRequest, "validation"),
            (ImageUploadRequest, "validation"),
            (ModerationResult, "serialization"),
        ],
        ref_template="#/components/schemas/{model}",
    )
    app.openapi().setdefault("components", {}).setdefault("schemas", {}).update(
        definitions["$defs"]
    )
