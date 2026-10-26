from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from modera.config import Settings
from modera.downloads import ImageDownloader
from modera.engine import ModerationEngine
from modera.errors import (
    ErrorResponse,
    RequestContextMiddleware,
    http_error_handler,
    validation_error_handler,
)
from modera.health import router as health_router
from modera.images import ImageValidator
from modera.moderations import register_contract_schemas
from modera.moderations import router as moderation_router
from modera.policies import load_policies


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.engine = ModerationEngine(app.state.settings, app.state.policies)
    app.state.images = ImageValidator()
    app.state.downloads = ImageDownloader(app.state.image_allowed_hosts)
    try:
        await app.state.engine.start()
        app.state.ready = True
        yield
    finally:
        app.state.ready = False
        app.state.engine.close()
        await app.state.downloads.close()
        app.state.downloads = None
        app.state.images.close()
        app.state.images = None


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()
    app = FastAPI(
        title="Modera — Text and Photo Moderation Service",
        version=version("modera"),
        lifespan=lifespan,
        docs_url="/docs" if config.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.docs_enabled else None,
        responses={
            500: {"model": ErrorResponse, "description": "Unexpected server error"}
        },
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.state.ready = False
    app.state.settings = config
    app.state.api_key = config.api_key
    app.state.text_model = config.text_model
    app.state.image_model = config.image_model
    app.state.policies = load_policies(config.policy_file)
    app.state.images = None
    app.state.downloads = None
    app.state.image_allowed_hosts = config.image_allowed_hosts
    app.include_router(health_router)
    app.include_router(moderation_router)
    register_contract_schemas(app)
    return app
