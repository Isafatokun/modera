from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI

from modera.config import Settings
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
    )
    app.state.ready = False
    app.include_router(health_router)
    app.include_router(moderation_router)
    register_request_schemas(app)
    return app
