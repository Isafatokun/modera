from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from modera.config import Settings
from modera.errors import (
    ErrorResponse,
    RequestContextMiddleware,
    http_error_handler,
    validation_error_handler,
)
from modera.health import router as health_router
from modera.moderations import register_request_schemas
from modera.moderations import router as moderation_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.ready = True
    try:
        yield
    finally:
        app.state.ready = False


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()
    app = FastAPI(
        title="Modera — Text and Photo Moderation Service",
        version=version("modera"),
        lifespan=lifespan,
        docs_url="/docs" if config.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.docs_enabled else None,
        responses={500: {"model": ErrorResponse, "description": "Unexpected server error"}},
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.state.ready = False
    app.include_router(health_router)
    app.include_router(moderation_router)
    register_request_schemas(app)
    return app
