from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from modera.admission import RequestCapacity
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
    async with AsyncExitStack() as resources:
        try:
            engine = ModerationEngine(app.state.settings, app.state.policies)
            resources.callback(engine.close)
            app.state.engine = engine
            images = ImageValidator()
            resources.callback(images.close)
            app.state.images = images
            downloads = ImageDownloader(app.state.image_allowed_hosts)
            resources.push_async_callback(downloads.close)
            app.state.downloads = downloads
            await engine.start()
            app.state.ready = True
            yield
        finally:
            app.state.ready = False
            app.state.engine = None
            app.state.images = None
            app.state.downloads = None


def create_app(settings: Settings | None = None) -> FastAPI:
    config = (settings if settings is not None else Settings()).model_copy(deep=True)
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
    app.state.request_capacity = RequestCapacity(config.max_in_flight_requests)
    app.state.settings = config
    app.state.api_key = config.api_key
    app.state.text_model = config.text_model
    app.state.image_model = config.image_model
    app.state.policies = load_policies(config.policy_file)
    app.state.engine = None
    app.state.images = None
    app.state.downloads = None
    app.state.image_allowed_hosts = config.image_allowed_hosts
    app.include_router(health_router)
    app.include_router(moderation_router)
    register_contract_schemas(app)
    return app
