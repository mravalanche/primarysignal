"""Administration web application composition."""

from fastapi import FastAPI

from primary_signal.config import Settings
from primary_signal.web.admin.health import router as admin_health_router
from primary_signal.web.common import Surface, create_base_app
from primary_signal.web.common.health import router as health_router
from primary_signal.web.common.templates import TemplateSurface
from primary_signal.web.common.ui import mount_ui_assets


def create_admin_app(settings: Settings | None = None) -> FastAPI:
    """Create the administration application and its explicit route set."""

    app = create_base_app(settings=settings or Settings(), surface=Surface.ADMIN)
    mount_ui_assets(app, TemplateSurface.ADMIN)
    app.include_router(health_router)
    app.include_router(admin_health_router)
    return app
