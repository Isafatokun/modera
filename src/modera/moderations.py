import asyncio
from collections.abc import AsyncGenerator
from typing import NoReturn

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import ValidationError
from pydantic.json_schema import models_json_schema
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser

from modera.auth import authenticate
from modera.errors import APIError, ErrorResponse, validation_error
from modera.policies import InputType, resolve_policy
from modera.results import ModerationResult
from modera.schemas import MAX_IMAGE_BYTES, ImageUploadRequest, ModerationRequest

MAX_JSON_BYTES = 32 * 1024
MAX_MULTIPART_BYTES = 9 * 1024 * 1024
BODY_TIMEOUT_SECONDS = 15

router = APIRouter(prefix="/v1", tags=["moderations"], dependencies=[Depends(authenticate)])


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


async def validate_upload(request: Request) -> str:
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
            raise APIError(400, "invalid_request", "Duplicate form fields are not allowed.")
        upload = ImageUploadRequest.model_validate(dict(form))
        if not upload.file.size:
            raise APIError(
                400, "invalid_request", "The image file must not be empty.", param="file"
            )
        if upload.file.size > MAX_IMAGE_BYTES:
            raise APIError(413, "payload_too_large", "The image file is too large.", param="file")
        return upload.policy
    finally:
        await form.close()


@router.post(
    "/moderations",
    status_code=501,
    response_model=None,
    summary="Moderate text or an image",
    description="Request validation only. Valid requests return 501 until moderation is available.",
    responses={
        status: {"model": ErrorResponse, "description": description}
        for status, description in {
            400: "Malformed body or invalid request fields",
            401: "Missing or invalid bearer API key",
            408: "Request body deadline exceeded",
            413: "Request or image file exceeds its size limit",
            415: "Unsupported request content type or content encoding",
            501: "Moderation is not implemented",
        }.items()
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ModerationRequest"}},
                "multipart/form-data": {
                    "schema": {"$ref": "#/components/schemas/ImageUploadRequest"}
                },
            },
        }
    },
)
async def moderate(request: Request) -> NoReturn:
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise APIError(
            415,
            "unsupported_content_encoding",
            "Content encoding is not supported.",
            headers={"Accept-Encoding": "identity"},
        )
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
            match media_type:
                case "application/json":
                    payload = await validate_json(request)
                    policy_name = payload.policy
                    input_type: InputType = payload.input.type
                case "multipart/form-data":
                    policy_name = await validate_upload(request)
                    input_type = "image"
                case _:
                    raise APIError(
                        415,
                        "unsupported_media_type",
                        "The request content type is not supported.",
                        headers={"Accept": "application/json, multipart/form-data"},
                    )
    except TimeoutError as exc:
        raise APIError(
            408, "request_timeout", "The request body deadline was exceeded.", retryable=True
        ) from exc
    except ValidationError as exc:
        raise validation_error(exc) from exc
    if request.app.state.policies:
        resolve_policy(request.app.state.policies, policy_name, input_type)
    raise APIError(501, "moderation_not_implemented", "Moderation is not implemented.")


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
