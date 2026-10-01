"""Minimal administration-surface liveness route."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminLivenessResponse(BaseModel):
    """Response for the admin-specific route manifest probe."""

    status: Literal["ok"] = "ok"


@router.get(
    "/health/live",
    name="admin_liveness",
    response_model=AdminLivenessResponse,
)
async def admin_liveness() -> AdminLivenessResponse:
    """Confirm that the administration route set is mounted."""

    return AdminLivenessResponse()
