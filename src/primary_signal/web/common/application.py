"""Shared FastAPI application construction."""

from enum import StrEnum

from fastapi import FastAPI

from primary_signal.config import Settings


class Surface(StrEnum):
    """Web surfaces that are deployed as separate processes."""

    PUBLIC = "public"
    ADMIN = "admin"


def create_base_app(*, settings: Settings, surface: Surface) -> FastAPI:
    """Create an un-routed application for one web surface."""

    docs_url = "/docs" if settings.api_docs_enabled else None
    openapi_url = "/openapi.json" if settings.api_docs_enabled else None

    app = FastAPI(
        title=f"{settings.service_name} {surface.value}",
        debug=settings.debug,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
    )
    app.state.settings = settings
    app.state.surface = surface
    return app
