"""Public web application composition."""

from fastapi import FastAPI

from primary_signal.config import Settings
from primary_signal.web.common import Surface, create_base_app
from primary_signal.web.common.health import router as health_router


def create_public_app(settings: Settings | None = None) -> FastAPI:
    """Create the internet-facing application without administration routes."""

    app = create_base_app(settings=settings or Settings(), surface=Surface.PUBLIC)
    app.include_router(health_router)
    return app
