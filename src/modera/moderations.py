import asyncio
from collections.abc import AsyncGenerator
from typing import NoReturn

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import ValidationError
from pydantic.json_schema import models_json_schema
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser

from modera.schemas import MAX_IMAGE_BYTES, ImageUploadRequest, ModerationRequest

MAX_JSON_BYTES = 32 * 1024
MAX_MULTIPART_BYTES = 9 * 1024 * 1024
BODY_TIMEOUT_SECONDS = 15

router = APIRouter(prefix="/v1", tags=["moderations"])


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
            raise HTTPException(status_code=413, detail="request body is too large")
        yield chunk


async def validate_json(request: Request) -> None:
    body = bytearray()
    async for chunk in limited_stream(request, MAX_JSON_BYTES):
        body.extend(chunk)
    ModerationRequest.model_validate_json(body)


async def validate_upload(request: Request) -> None:
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
        raise HTTPException(status_code=400, detail="invalid multipart body") from exc
    try:
        if len(form.multi_items()) != len(form):
            raise HTTPException(status_code=400, detail="duplicate form fields are not allowed")
        upload = ImageUploadRequest.model_validate(dict(form))
        if not upload.file.size:
            raise HTTPException(status_code=400, detail="image file must not be empty")
        if upload.file.size > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="image file is too large")
    finally:
        await form.close()


@router.post(
    "/moderations",
    status_code=501,
    response_model=None,
    summary="Moderate text or an image",
    description="Request validation only. Valid requests return 501 until moderation is available.",
    responses={
        400: {"description": "Malformed body or invalid request fields"},
        408: {"description": "Request body deadline exceeded"},
        413: {"description": "Request or image file exceeds its size limit"},
        415: {"description": "Unsupported request content type or content encoding"},
        501: {"description": "Moderation is not implemented"},
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
        raise HTTPException(status_code=415, detail="content encoding is not supported")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
            match media_type:
                case "application/json":
                    await validate_json(request)
                case "multipart/form-data":
                    await validate_upload(request)
                case _:
                    raise HTTPException(status_code=415, detail="content type is not supported")
    except TimeoutError as exc:
        raise HTTPException(status_code=408, detail="request body deadline exceeded") from exc
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        for error in errors:
            error["loc"] = ("body", *error["loc"])
        raise HTTPException(status_code=400, detail=errors) from exc
    raise HTTPException(status_code=501, detail="moderation is not implemented")


def register_request_schemas(app: FastAPI) -> None:
    _, definitions = models_json_schema(
        [(ModerationRequest, "validation"), (ImageUploadRequest, "validation")],
        ref_template="#/components/schemas/{model}",
    )
    app.openapi().setdefault("components", {}).setdefault("schemas", {}).update(
        definitions["$defs"]
    )
