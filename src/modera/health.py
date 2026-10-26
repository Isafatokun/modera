from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from modera.errors import APIError, ErrorResponse

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@router.get("/live")
async def liveness() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/ready",
    responses={503: {"model": ErrorResponse, "description": "Service is not ready"}},
)
async def readiness(request: Request) -> HealthResponse:
    if not request.app.state.ready:
        raise APIError(
            503,
            "service_not_ready",
            "The service is not ready.",
            retryable=True,
            headers={"Retry-After": "1"},
        )
    return HealthResponse()
