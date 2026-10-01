"""Shared liveness endpoint."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from primary_signal.web.common.application import Surface

router = APIRouter()


class LivenessResponse(BaseModel):
    """Minimal response that does not expose deployment details."""

    status: Literal["ok"] = "ok"
    surface: Surface


@router.get("/health/live", name="liveness", response_model=LivenessResponse)
async def liveness(request: Request) -> LivenessResponse:
    """Confirm that the selected web process can answer requests."""

    return LivenessResponse(surface=request.app.state.surface)
