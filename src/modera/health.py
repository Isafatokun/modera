from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@router.get("/live")
async def liveness() -> HealthResponse:
    return HealthResponse()


@router.get("/ready", responses={503: {"description": "Service is not ready"}})
async def readiness(request: Request) -> HealthResponse:
    if not request.app.state.ready:
        raise HTTPException(status_code=503, detail="service is not ready")
    return HealthResponse()
