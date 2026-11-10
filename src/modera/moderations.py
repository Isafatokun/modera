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


def require_backend(
    request: Request, engine: ModerationEngine | None, input_type: InputType
) -> ModerationEngine:
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
    return engine


def resolve_json(payload: ModerationRequest) -> ResolvedInput:
    if isinstance(payload.input, TextInput):
        return ResolvedInput(payload.policy, InputSource.TEXT, payload.input.text)
    return ResolvedInput(
        payload.policy, InputSource.IMAGE_URL, str(payload.input.source.url)
    )


async def read_input(request: Request) -> ResolvedInput:
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
                    return resolve_json(await validate_json(request))
                case "multipart/form-data":
                    policy_name, image_data = await validate_upload(request)
                    return ResolvedInput(
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


async def fetch_image(
    request: Request, resolved: ResolvedInput, engine: ModerationEngine | None
) -> bytes:
    if resolved.source is InputSource.IMAGE_UPLOAD:
        if not isinstance(resolved.content, bytes):
            raise TypeError("uploaded input must contain bytes")
        return resolved.content
    downloader = request.app.state.downloads
    if downloader is None:
        raise APIError(
            503, "service_not_ready", "The service is not ready.", retryable=True
        )
    if not isinstance(resolved.content, str):
        raise TypeError("URL input must contain a URL string")
    url = HttpUrl(resolved.content)
    downloader.check_allowed(url)
    require_backend(request, engine, resolved.input_type)
    data: bytes = await downloader.fetch(url)
    return data


async def prepare_image(
    request: Request,
    data: bytes,
    engine: ModerationEngine | None,
    *,
    from_url: bool,
) -> bytes | PreparedImage:
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
            await validator.validate(data)
            return data
        prepared: PreparedImage = await validator.prepare(data, preprocessor)
        return prepared
    except APIError as exc:
        if from_url and exc.param == "file":
            exc.param = "input.source.url"
        raise


async def prepare_content(
    request: Request, resolved: ResolvedInput, engine: ModerationEngine | None
) -> str | bytes | PreparedImage:
    if resolved.source is InputSource.TEXT:
        return resolved.content
    data = await fetch_image(request, resolved, engine)
    return await prepare_image(
        request, data, engine, from_url=resolved.source is InputSource.IMAGE_URL
    )


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
    resolved = await read_input(request)
    if request.app.state.policies:
        resolve_policy(request.app.state.policies, resolved.policy, resolved.input_type)
    engine: ModerationEngine | None = getattr(request.app.state, "engine", None)
    content = await prepare_content(request, resolved, engine)
    engine = require_backend(request, engine, resolved.input_type)
    policy = resolve_policy(
        request.app.state.policies, resolved.policy, resolved.input_type
    )
    result = await engine.evaluate(
        resolved.input_type, content, policy, request.state.request_id
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
